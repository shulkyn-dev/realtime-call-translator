"""Захват системного звука (WASAPI loopback) + нарезка на фразы по тишине.

Слушает то, что ты СЛЫШИШЬ (голоса собеседников в Discord/браузере/любом
приложении), приводит к моно 16 кГц и отдаёт готовые фразы в очередь.
Дополнительно шлёт в status_queue уровень сигнала — чтобы в UI была видна
«шкала звука» и было понятно, что захват реально работает.
"""
import threading
import queue
import time
import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

import config


def list_loopback_devices():
    """Список loopback-устройств вывода для выпадающего списка в UI.

    Возвращает [(index, name, is_default), ...]. is_default — устройство,
    соответствующее текущему динамику/наушникам по умолчанию.
    """
    pa = pyaudio.PyAudio()
    try:
        default_out = pa.get_device_info_by_index(
            pa.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultOutputDevice"]
        )
        default_name = default_out["name"]
        result = []
        for lb in pa.get_loopback_device_info_generator():
            is_default = default_name in lb["name"]
            result.append((lb["index"], lb["name"], is_default))
        return result
    finally:
        pa.terminate()


def resolve_default_loopback_index():
    """Индекс loopback-устройства, соответствующего текущему выводу по умолчанию."""
    for idx, _name, is_default in list_loopback_devices():
        if is_default:
            return idx
    devs = list_loopback_devices()
    return devs[0][0] if devs else None


class AudioSegmenter(threading.Thread):
    """Фоновый поток: loopback-запись → энергетический VAD → очередь фраз."""

    def __init__(self, utterance_queue: queue.Queue, stop_event: threading.Event,
                 status_queue: queue.Queue = None, device_index: int = None):
        super().__init__(daemon=True)
        self.out = utterance_queue          # сюда кладём np.float32 (16k mono)
        self.stop_event = stop_event
        self.status = status_queue          # ('level', rms) / ('utterance', sec) / ('error', msg)
        self.device_index = device_index    # None → устройство по умолчанию
        self._pa = pyaudio.PyAudio()

    def _emit(self, kind, value):
        if self.status is not None:
            try:
                self.status.put_nowait((kind, value))
            except queue.Full:
                pass

    def _get_device(self):
        if self.device_index is not None:
            return self._pa.get_device_info_by_index(self.device_index)
        default_speakers = self._pa.get_device_info_by_index(
            self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultOutputDevice"]
        )
        if not default_speakers.get("isLoopbackDevice", False):
            for lb in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in lb["name"]:
                    return lb
        return default_speakers

    def run(self):
        try:
            dev = self._get_device()
            rate = int(dev["defaultSampleRate"])
            channels = int(dev["maxInputChannels"])
            frames_per_buffer = int(rate * config.FRAME_MS / 1000)
            self._emit("device", dev["name"])

            # сразу после переключения устройства (например, Bluetooth-наушники
            # только что подключились) оно иногда на секунду занято — пробуем
            # несколько раз, прежде чем показывать ошибку
            stream = None
            last_err = None
            for attempt in range(3):
                try:
                    stream = self._pa.open(
                        format=pyaudio.paFloat32,
                        channels=channels,
                        rate=rate,
                        frames_per_buffer=frames_per_buffer,
                        input=True,
                        input_device_index=dev["index"],
                    )
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(1.0)
            if stream is None:
                raise last_err
        except Exception as e:
            self._emit("error", f"Failed to open audio: {e}")
            return

        hang_frames = int(config.SILENCE_HANG_MS / config.FRAME_MS)
        min_frames = int(config.MIN_UTTERANCE_MS / config.FRAME_MS)
        max_frames = int(config.MAX_UTTERANCE_MS / config.FRAME_MS)
        interim_frames = int(config.INTERIM_INTERVAL_MS / config.FRAME_MS)

        buffer = []
        silence_run = 0
        in_speech = False
        frames_since_interim = 0
        last_level_emit = 0.0

        try:
            while not self.stop_event.is_set():
                raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                audio = np.frombuffer(raw, dtype=np.float32)
                if channels > 1:
                    audio = audio.reshape(-1, channels).mean(axis=1)
                if rate != config.SAMPLE_RATE:
                    audio = resample_poly(audio, config.SAMPLE_RATE, rate).astype(np.float32)

                rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
                is_speech = rms >= config.SILENCE_RMS

                # шкала уровня в UI — не чаще ~10 раз/сек
                now = time.time()
                if now - last_level_emit > 0.1:
                    self._emit("level", rms)
                    last_level_emit = now

                if is_speech:
                    buffer.append(audio)
                    silence_run = 0
                    in_speech = True
                elif in_speech:
                    buffer.append(audio)
                    silence_run += 1

                # «черновой» английский на лету, пока фраза ещё копится
                if config.INTERIM_ENABLED and in_speech and len(buffer) >= min_frames:
                    frames_since_interim += 1
                    if frames_since_interim >= interim_frames:
                        self.out.put(("interim", np.concatenate(buffer)))
                        frames_since_interim = 0

                end_by_silence = in_speech and silence_run >= hang_frames
                end_by_length = len(buffer) >= max_frames

                if (end_by_silence or end_by_length) and len(buffer) >= min_frames:
                    utt = np.concatenate(buffer)
                    self._emit("utterance", len(utt) / config.SAMPLE_RATE)
                    self.out.put(("final", utt))
                    buffer, silence_run, in_speech, frames_since_interim = [], 0, False, 0
                elif end_by_silence:
                    buffer, silence_run, in_speech, frames_since_interim = [], 0, False, 0
        except Exception as e:
            self._emit("error", f"Capture error: {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            self._pa.terminate()
