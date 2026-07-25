"""Компактная панель: субтитры созвона EN→RU в реальном времени + задел под ИИ.

Три секции (каждую можно включать/выключать):
  🗣 Английский  — живой поток речи клиента (обновляется на лету)
  🇷🇺 Перевод     — русский, копится историей, прокручивается
  🤖 Подсказка ИИ — что ответить (пока заглушка; позже — агент с базой знаний)

Окно всегда поверх других, двигается мышью за верхнюю полосу, тянется за уголок.
"""
import os
import sys
import time
import queue
import threading
import base64

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtPrintSupport import QPrinter

import config
import paths
from audio import AudioSegmenter, list_loopback_devices
from pipeline import Transcriber
from sessionlog import SessionLog
from assistant import InterviewAssistant
from text_translate import TARGET_LANGS, translate_async
from cover_letter import send_analysis_async, send_letter_async
from flags import flag_pixmap
from settings_dialog import KeysDialog
import cuda_setup

CARD = "rgba(22,22,30,238)"
HISTORY_MAX = 200          # сколько строк держать в каждой панели
ICON_PATH = paths.resource_path(os.path.join("assets", "icon.ico"))


def _qimage_to_png_b64(qimage: QtGui.QImage) -> str:
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    qimage.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return base64.b64encode(data).decode("ascii")


class ImagePasteTextEdit(QtWidgets.QTextEdit):
    """QTextEdit, который умеет принимать картинку (paste из буфера обмена —
    например скриншот Win+Shift+S — или drag&drop файла) вместо того, чтобы
    вставлять её как rich-text: картинка уходит через on_image_callback,
    обычный текст ведёт себя как всегда."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_image_callback = None
        self.setAcceptDrops(True)

    def insertFromMimeData(self, source):
        if source.hasImage() and self.on_image_callback:
            data = source.imageData()
            qimage = data if isinstance(data, QtGui.QImage) else QtGui.QImage(data)
            if not qimage.isNull():
                self.on_image_callback(qimage)
                return
        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasImage() or md.hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasImage() and self.on_image_callback:
            data = md.imageData()
            qimage = data if isinstance(data, QtGui.QImage) else QtGui.QImage(data)
            if not qimage.isNull():
                self.on_image_callback(qimage)
                event.acceptProposedAction()
                return
        if md.hasUrls():
            for url in md.urls():
                path = url.toLocalFile()
                if path.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp")):
                    qimage = QtGui.QImage(path)
                    if not qimage.isNull() and self.on_image_callback:
                        self.on_image_callback(qimage)
                        event.acceptProposedAction()
                        return
        super().dropEvent(event)


class TranslatorApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        # иконка окна/таскбара — путь строим от __file__, не от cwd (иначе
        # ярлык/launch.vbs с другим рабочим каталогом её не найдёт)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(ICON_PATH))

        # ключ Anthropic обязателен (Text Translate, Cover Letter, ИИ-подсказки
        # без него не работают) — если его нет в .env, просим ввести до того,
        # как строится UI; QApplication к этому моменту уже существует (run())
        if not config.ANTHROPIC_API_KEY:
            KeysDialog(None).exec()

        # состояние текста
        self.en_history = []       # финальные англ. фразы
        self.ru_history = []       # финальные рус. фразы
        self.live_en = ""          # черновой англ. (обновляется на лету)
        self.live_ru = ""          # черновой рус. перевод (провизорный)
        self.ai_history = []       # подсказки ИИ (история)
        self.ai_thinking = False
        self.font_ru = config.FONT_SIZE_RU
        self.font_en = config.FONT_SIZE_RU   # все панели одного «среднего» размера
        self._drag_pos = None

        # рабочие потоки/очереди
        self.stop_event = None
        self.utterance_queue = None
        self.subtitle_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.transcriber = None
        self.segmenter = None
        self.ai_input_queue = None
        self.ai_output_queue = queue.Queue()
        self.ai_assistant = None
        self.running = False
        self._last_sound_time = None  # monotonic-время последнего РЕАЛЬНОГО звука выше порога — для авто-стопа
        self.session_log = SessionLog()
        self.translate_result_queue = queue.Queue()
        self._tr_last_source = None   # язык, который определил Claude — для кнопки Swap
        self.tr_pending_image = None  # base64 PNG скриншота, ждущего перевода (Text Translate)
        self.cover_result_queue = queue.Queue()
        self.cover_messages = []      # Anthropic messages — история диалога про текущую вакансию
        self.cover_history = []       # строки для отображения (You/Bot)
        self.cover_thinking = False
        self.cover_last_letter = ""   # последнее письмо целиком — для кнопки Copy
        self.cover_pending_image = None  # base64 PNG скриншота, ждущего отправки в чат
        self.cover_pending_mode = "analyze"  # "analyze" или "write" — какой запрос сейчас в полёте
        self._btn_after_cover = None  # (кнопка, текст) — что вернуть после ответа бота
        self._cover_dirty = False     # перерисовывать панель диалога только когда что-то изменилось

        # без Tool — чтобы было место в панели задач и работало «Свернуть»
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("Realtime Translator")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

        self._build_ui()
        self._position_right()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)

    # ---------- UI ----------
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.card = QtWidgets.QFrame()
        self.card.setObjectName("card")
        self.card.setStyleSheet(f"QFrame#card {{ background: {CARD}; border-radius: 14px; }}")
        root.addWidget(self.card)

        v = QtWidgets.QVBoxLayout(self.card)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(8)

        # --- верхняя полоса ---
        top = QtWidgets.QHBoxLayout()
        self.dot = QtWidgets.QLabel("●")
        self._set_dot("stopped")
        title = QtWidgets.QLabel("Realtime Translator")
        title.setStyleSheet("color:#eaeaf0; font-size:13px; font-weight:600;")
        self.state_lbl = QtWidgets.QLabel("")
        self.state_lbl.setStyleSheet("color:#9aa0b5; font-size:11px;")
        self.keys_btn = self._mk_win_btn("⚙", "#eaeaf0", self._open_keys_dialog)
        self.keys_btn.setToolTip("API keys")
        self.min_btn = self._mk_win_btn("—", "#eaeaf0", self._minimize)
        self.max_btn = self._mk_win_btn("▢", "#eaeaf0", self._toggle_max)
        close_btn = self._mk_win_btn("✕", "#ff6b6b", self.close)
        top.addWidget(self.dot)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.state_lbl)
        top.addSpacing(6)
        top.addWidget(self.keys_btn)
        top.addWidget(self.min_btn)
        top.addWidget(self.max_btn)
        top.addWidget(close_btn)
        v.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane{border:none;}"
            "QTabBar::tab{background:rgba(255,255,255,12);color:#9aa0b5;"
            "padding:5px 12px;border-top-left-radius:6px;border-top-right-radius:6px;"
            "font-size:11px;margin-right:2px;}"
            "QTabBar::tab:selected{background:rgba(255,255,255,24);color:#eaeaf0;}"
        )
        self.tabs.addTab(self._build_live_tab(), "Live Call")
        self.tabs.addTab(self._build_translate_tab(), "Text Translate")
        self.tabs.addTab(self._build_cover_letter_tab(), "Cover Letter")
        v.addWidget(self.tabs, 1)

    def _build_live_tab(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        # --- шкала уровня ---
        self.level = QtWidgets.QProgressBar()
        self.level.setRange(0, 100)
        self.level.setValue(0)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(5)
        self.level.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,20);border-radius:3px;}"
            "QProgressBar::chunk{background:#4caf7d;border-radius:3px;}"
        )
        v.addWidget(self.level)

        # --- три панели в сплиттере (тянутся мышью) ---
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.setStyleSheet(
            "QSplitter::handle{background:rgba(255,255,255,20);height:3px;}"
        )
        self.en_panel, self.en_edit = self._mk_panel("English (client)",
                                                     False, "#b7bcce", flag_code="gb")
        self.ru_panel, self.ru_edit = self._mk_panel("Translation",
                                                     True, "#ffffff", bold=True, flag_code="ru")
        self.ai_panel, self.ai_edit = self._mk_panel("🤖 AI Suggestion",
                                                     False, "#8fd6b4")
        self._apply_fonts()
        self._set_placeholder(self.ai_edit, "Waiting for a question — the suggestion will appear here automatically.")
        self.splitter.addWidget(self.en_panel)
        self.splitter.addWidget(self.ru_panel)
        self.splitter.addWidget(self.ai_panel)
        self.splitter.setSizes([160, 320, 160])
        v.addWidget(self.splitter, 1)

        # --- панель управления ---
        ctrl = QtWidgets.QHBoxLayout()
        ctrl.setSpacing(6)
        self.start_btn = QtWidgets.QPushButton("▶ Start")
        self.start_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._style_start()
        self.start_btn.clicked.connect(self.toggle)
        ctrl.addWidget(self.start_btn)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setStyleSheet(self._combo_style())
        self._fill_devices()
        ctrl.addWidget(self.device_combo, 1)

        self.en_check = QtWidgets.QCheckBox("EN")
        self.en_check.setChecked(True)
        self.en_check.setStyleSheet("color:#9aa0b5;font-size:11px;")
        self.en_check.stateChanged.connect(lambda s: self.en_panel.setVisible(bool(s)))
        ctrl.addWidget(self.en_check)

        self.ai_check = QtWidgets.QCheckBox("AI")
        self.ai_check.setChecked(True)
        self.ai_check.setStyleSheet("color:#9aa0b5;font-size:11px;")
        self.ai_check.stateChanged.connect(self._toggle_ai)
        ctrl.addWidget(self.ai_check)

        self.log_check = QtWidgets.QCheckBox("Log")
        self.log_check.setChecked(True)
        self.log_check.setStyleSheet("color:#9aa0b5;font-size:11px;")
        ctrl.addWidget(self.log_check)

        self.gear = QtWidgets.QPushButton("⚙")
        self.gear.setFixedSize(24, 24)
        self.gear.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.gear.setStyleSheet(
            "QPushButton{color:#9aa0b5;border:none;font-size:14px;}"
            "QPushButton:hover{color:#eaeaf0;}"
        )
        self.gear.clicked.connect(self._toggle_settings)
        ctrl.addWidget(self.gear)
        v.addLayout(ctrl)

        # --- расширенные настройки ---
        self.settings = QtWidgets.QFrame()
        sv = QtWidgets.QHBoxLayout(self.settings)
        sv.setContentsMargins(0, 2, 0, 2)
        sv.setSpacing(8)
        sv.addWidget(self._mk_label("Model:"))
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems(["large-v3", "medium", "small"])
        self.model_combo.setCurrentText(config.MODEL_SIZE)
        self.model_combo.setStyleSheet(self._combo_style())
        sv.addWidget(self.model_combo)
        sv.addWidget(self._mk_label("Language:"))
        self.lang_combo = QtWidgets.QComboBox()
        self._lang_codes = ["auto", "en", "ru", "uk"]
        self.lang_combo.addItems(["Auto (detect)", "English", "Russian", "Ukrainian"])
        self.lang_combo.setCurrentIndex(self._lang_codes.index(config.WHISPER_LANG))
        self.lang_combo.setStyleSheet(self._combo_style())
        self.lang_combo.setToolTip(
            "“Auto” detects the language automatically for every phrase. Pick a"
            " specific language only if auto-detection gets it wrong."
        )
        sv.addWidget(self.lang_combo)
        sv.addWidget(self._mk_label("Font:"))
        self.font_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.font_slider.setRange(11, 30)
        self.font_slider.setValue(self.font_ru)
        self.font_slider.valueChanged.connect(self._on_font)
        sv.addWidget(self.font_slider, 1)
        self.font_val = self._mk_label(str(self.font_ru))
        sv.addWidget(self.font_val)
        sv.addWidget(self._mk_label("Threshold:"))
        self.thr_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.thr_slider.setRange(2, 40)
        self.thr_slider.setValue(int(config.SILENCE_RMS * 1000))
        self.thr_slider.valueChanged.connect(self._on_threshold)
        sv.addWidget(self.thr_slider, 1)
        self.thr_val = self._mk_label(f"{config.SILENCE_RMS:.3f}")
        sv.addWidget(self.thr_val)
        self.settings.setVisible(False)
        v.addWidget(self.settings)

        # --- строка лога ---
        log_row = QtWidgets.QHBoxLayout()
        self.log_lbl = QtWidgets.QLabel("Log: not started")
        self.log_lbl.setStyleSheet("color:#6b7089;font-size:10px;")
        log_row.addWidget(self.log_lbl)
        log_row.addStretch()
        open_logs_btn = QtWidgets.QPushButton("Logs folder")
        open_logs_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        open_logs_btn.setStyleSheet(
            "QPushButton{color:#6b7089;border:none;font-size:10px;text-decoration:underline;}"
            "QPushButton:hover{color:#9aa0b5;}"
        )
        open_logs_btn.clicked.connect(self._open_logs_folder)
        log_row.addWidget(open_logs_btn)
        v.addLayout(log_row)

        # --- уголок для изменения размера ---
        grip_row = QtWidgets.QHBoxLayout()
        grip_row.addStretch()
        grip = QtWidgets.QSizeGrip(self)
        grip.setStyleSheet("width:14px;height:14px;")
        grip_row.addWidget(grip)
        v.addLayout(grip_row)

        self.ai_panel.setVisible(self.ai_check.isChecked())
        return page

    def _build_translate_tab(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._mk_label("Translate to:"))
        self.tr_target_combo = QtWidgets.QComboBox()
        self.tr_target_combo.addItems(TARGET_LANGS)
        self.tr_target_combo.setCurrentText("Russian")
        self.tr_target_combo.setStyleSheet(self._combo_style())
        row.addWidget(self.tr_target_combo)
        self.tr_swap_btn = QtWidgets.QPushButton("⇄ Swap")
        self.tr_swap_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.tr_swap_btn.setToolTip(
            "Moves the translation into the input box, so you can pick a new"
            " target language and translate back the other way."
        )
        self.tr_swap_btn.setStyleSheet(self._utility_btn_style())
        self.tr_swap_btn.clicked.connect(self._on_swap_click)
        row.addWidget(self.tr_swap_btn)
        row.addStretch()
        v.addLayout(row)

        in_header = QtWidgets.QLabel("Type or paste text — or paste/drop a screenshot (any language)")
        in_header.setStyleSheet("color:#6b7089;font-size:10px;font-weight:600;")
        v.addWidget(in_header)

        # --- индикатор вложенного скриншота — тот же паттерн, что в Cover
        # Letter: зелёная подпись + крестик сброса ---
        tr_attach_row = QtWidgets.QHBoxLayout()
        self.tr_attach_label = QtWidgets.QLabel("")
        self.tr_attach_label.setStyleSheet("color:#8fd6ac;font-size:10px;font-weight:600;")
        self.tr_attach_label.setVisible(False)
        tr_attach_row.addWidget(self.tr_attach_label)
        self.tr_attach_clear_btn = QtWidgets.QPushButton("✕")
        self.tr_attach_clear_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.tr_attach_clear_btn.setStyleSheet(self._utility_btn_style())
        self.tr_attach_clear_btn.setFixedWidth(28)
        self.tr_attach_clear_btn.setVisible(False)
        self.tr_attach_clear_btn.clicked.connect(self._on_tr_clear_image)
        tr_attach_row.addWidget(self.tr_attach_clear_btn)
        tr_attach_row.addStretch()
        v.addLayout(tr_attach_row)

        self.tr_input = ImagePasteTextEdit()
        self.tr_input.setPlaceholderText("Type or paste text here… (or paste/drop a screenshot)")
        self.tr_input.on_image_callback = self._on_tr_image_attached
        self.tr_input.textChanged.connect(self._on_translate_input_changed)
        v.addWidget(self.tr_input, 1)

        btn_row = QtWidgets.QHBoxLayout()
        self.tr_btn = QtWidgets.QPushButton("Translate  (Ctrl+Enter)")
        self.tr_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.tr_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:6px 14px;font-weight:600;}QPushButton:hover{background:#379268;}"
        )
        self.tr_btn.clicked.connect(self._on_translate_click)
        btn_row.addWidget(self.tr_btn)

        self.tr_copy_btn = QtWidgets.QPushButton("📋 Copy")
        self.tr_copy_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.tr_copy_btn.setStyleSheet(self._utility_btn_style())
        self.tr_copy_btn.clicked.connect(
            lambda: self._copy_text(self.tr_output.toPlainText(), self.tr_copy_btn)
        )
        btn_row.addWidget(self.tr_copy_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        out_header = QtWidgets.QLabel("Translation")
        out_header.setStyleSheet("color:#6b7089;font-size:10px;font-weight:600;")
        v.addWidget(out_header)
        self.tr_output = QtWidgets.QTextEdit()
        self.tr_output.setReadOnly(True)
        v.addWidget(self.tr_output, 1)

        self._apply_translate_fonts()

        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.tr_input)
        shortcut.activated.connect(self._on_translate_click)

        return page

    def _on_translate_click(self):
        target = self.tr_target_combo.currentText()
        # если есть вложенный скриншот — переводим его, текстовое поле в
        # этом случае игнорируется (пользователь скинул картинку — значит
        # именно её и хочет перевести)
        if self.tr_pending_image:
            self.tr_btn.setEnabled(False)
            self.tr_btn.setText("Translating…")
            self.tr_output.setPlainText("⏳ Translating…")
            translate_async("", target, self.translate_result_queue, image_b64=self.tr_pending_image)
            return
        text = self.tr_input.toPlainText().strip()
        if not text:
            return
        self.tr_btn.setEnabled(False)
        self.tr_btn.setText("Translating…")
        self.tr_output.setPlainText("⏳ Translating…")
        translate_async(text, target, self.translate_result_queue)

    def _on_tr_image_attached(self, qimage: QtGui.QImage):
        """Вызывается ImagePasteTextEdit при paste/drag&drop картинки в
        Text Translate — держим её как ожидающую перевода и сразу запускаем
        перевод (пользователь скинул скриншот — не нужен лишний клик)."""
        self.tr_pending_image = _qimage_to_png_b64(qimage)
        self.tr_attach_label.setText("📎 Screenshot attached — press Translate")
        self.tr_attach_label.setVisible(True)
        self.tr_attach_clear_btn.setVisible(True)
        self._on_translate_click()

    def _on_tr_clear_image(self):
        self.tr_pending_image = None
        self.tr_attach_label.setVisible(False)
        self.tr_attach_clear_btn.setVisible(False)

    def _copy_text(self, text, btn):
        if not text or text.startswith(("⏳", "⚠")):
            return
        QtWidgets.QApplication.clipboard().setText(text)
        btn.setText("✅ Copied")
        QtCore.QTimer.singleShot(1200, lambda: btn.setText("📋 Copy"))

    def _on_translate_input_changed(self):
        if not self.tr_input.toPlainText().strip():
            self.tr_output.clear()
            self._tr_last_source = None
        # ввод нового текста руками — значит вложенный скриншот больше не
        # актуален (paste/drop картинки сюда textChanged не вызывает, т.к.
        # ImagePasteTextEdit перехватывает её раньше вставки в документ)
        if self.tr_pending_image:
            self._on_tr_clear_image()

    def _on_swap_click(self):
        result = self.tr_output.toPlainText().strip()
        if not result or result.startswith(("⏳", "⚠")):
            return  # нечего переносить — ещё не переведено или там ошибка
        self.tr_input.setPlainText(result)
        self.tr_output.clear()
        # вложенный скриншот больше не относится к новому тексту в поле —
        # иначе повторный Translate снова перевёл бы старую картинку
        self._on_tr_clear_image()
        # переключаем направление на язык, который реально был исходным —
        # его сообщил Claude вместе с переводом, а не наше предположение
        if self._tr_last_source:
            idx = self.tr_target_combo.findText(self._tr_last_source)
            if idx >= 0:
                self.tr_target_combo.setCurrentIndex(idx)
        self._tr_last_source = None

    def _build_cover_letter_tab(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        chat_header_row = QtWidgets.QHBoxLayout()
        chat_header = QtWidgets.QLabel("Discuss the vacancy — paste it, ask questions, correct the bot")
        chat_header.setStyleSheet("color:#6b7089;font-size:10px;font-weight:600;")
        chat_header_row.addWidget(chat_header)
        chat_header_row.addStretch()
        self.cover_new_btn = QtWidgets.QPushButton("🗑 New")
        self.cover_new_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_new_btn.setStyleSheet(self._utility_btn_style())
        self.cover_new_btn.clicked.connect(self._on_cover_new_click)
        chat_header_row.addWidget(self.cover_new_btn)
        v.addLayout(chat_header_row)

        self.cover_chat_view = QtWidgets.QTextEdit()
        self.cover_chat_view.setReadOnly(True)
        # состояние для инкрементального дописывания — та же логика, что и
        # для панели ИИ-подсказок в Live Call (см. _update_panel)
        self.cover_chat_view._committed = 0
        self.cover_chat_view._live_active = False
        self.cover_chat_view._has_content = False
        self.cover_chat_view._live_start = 0
        self.cover_chat_view._live_sep_len = 0
        v.addWidget(self.cover_chat_view, 2)

        # --- строка вложения — прикрепить скриншот (вопросы из формы
        # заявки и т.п.) кнопкой, или просто вставить/перетащить в поле ниже ---
        attach_row = QtWidgets.QHBoxLayout()
        self.cover_attach_btn = QtWidgets.QPushButton("📎 Screenshot")
        self.cover_attach_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_attach_btn.setStyleSheet(self._utility_btn_style())
        self.cover_attach_btn.clicked.connect(self._on_cover_attach_click)
        attach_row.addWidget(self.cover_attach_btn)
        self.cover_attach_label = QtWidgets.QLabel("")
        self.cover_attach_label.setStyleSheet("color:#8fd6ac;font-size:10px;font-weight:600;")
        self.cover_attach_label.setVisible(False)
        attach_row.addWidget(self.cover_attach_label)
        self.cover_attach_clear_btn = QtWidgets.QPushButton("✕")
        self.cover_attach_clear_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_attach_clear_btn.setStyleSheet(self._utility_btn_style())
        self.cover_attach_clear_btn.setFixedWidth(28)
        self.cover_attach_clear_btn.setVisible(False)
        self.cover_attach_clear_btn.clicked.connect(self._on_cover_clear_image)
        attach_row.addWidget(self.cover_attach_clear_btn)
        attach_row.addStretch()
        v.addLayout(attach_row)

        input_row = QtWidgets.QHBoxLayout()
        self.cover_chat_input = ImagePasteTextEdit()
        self.cover_chat_input.on_image_callback = self._on_cover_image_attached
        self.cover_chat_input.setPlaceholderText(
            "Paste the vacancy, ask a question, or drop/paste a screenshot…  (Ctrl+Enter to send)"
        )
        self.cover_chat_input.setFixedHeight(70)
        input_row.addWidget(self.cover_chat_input, 1)
        self.cover_send_btn = QtWidgets.QPushButton("Send")
        self.cover_send_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_send_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:6px 14px;font-weight:600;}QPushButton:hover{background:#379268;}"
        )
        self.cover_send_btn.clicked.connect(self._on_cover_send_click)
        input_row.addWidget(self.cover_send_btn)
        v.addLayout(input_row)

        # --- отдельная кнопка "написать письмо" — жмётся, когда разбор в
        # чате выше устраивает; до первого ответа бота недоступна ---
        gen_row = QtWidgets.QHBoxLayout()
        gen_row.addStretch()
        self.cover_generate_btn = QtWidgets.QPushButton("✍ Generate Cover Letter")
        self.cover_generate_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_generate_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:6px 14px;font-weight:600;}QPushButton:hover{background:#379268;}"
            "QPushButton:disabled{background:#3a3f4d;color:#7a7f8f;}"
        )
        self.cover_generate_btn.setEnabled(False)  # включится после первого ответа бота
        self.cover_generate_btn.clicked.connect(self._on_cover_generate_click)
        gen_row.addWidget(self.cover_generate_btn)
        v.addLayout(gen_row)

        letter_header_row = QtWidgets.QHBoxLayout()
        letter_header = QtWidgets.QLabel("Cover Letter")
        letter_header.setStyleSheet("color:#6b7089;font-size:10px;font-weight:600;")
        letter_header_row.addWidget(letter_header)
        letter_header_row.addStretch()
        self.cover_copy_btn = QtWidgets.QPushButton("📋 Copy")
        self.cover_copy_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_copy_btn.setStyleSheet(self._utility_btn_style())
        self.cover_copy_btn.clicked.connect(
            lambda: self._copy_text(self.cover_last_letter, self.cover_copy_btn)
        )
        letter_header_row.addWidget(self.cover_copy_btn)
        self.cover_pdf_btn = QtWidgets.QPushButton("📄 Save PDF")
        self.cover_pdf_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cover_pdf_btn.setStyleSheet(self._utility_btn_style())
        self.cover_pdf_btn.clicked.connect(self._on_cover_save_pdf)
        letter_header_row.addWidget(self.cover_pdf_btn)
        v.addLayout(letter_header_row)

        self.cover_letter_output = QtWidgets.QTextEdit()
        self.cover_letter_output.setReadOnly(True)
        v.addWidget(self.cover_letter_output, 1)

        self._apply_cover_fonts()

        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.cover_chat_input)
        shortcut.activated.connect(self._on_cover_send_click)

        return page

    def _apply_cover_fonts(self):
        self.cover_chat_input.setStyleSheet(
            "QTextEdit{background:rgba(255,255,255,12);border:1px solid rgba(255,255,255,25);"
            f"border-radius:8px;color:#eaeaf0;font-size:{self.font_ru}px;padding:6px;}}"
        )
        self.cover_chat_view.setStyleSheet(
            "QTextEdit{background:rgba(255,255,255,8);border:1px solid rgba(255,255,255,18);"
            f"border-radius:8px;color:#ffffff;font-size:{self.font_ru}px;font-weight:600;padding:6px;}}"
        )
        self.cover_letter_output.setStyleSheet(
            "QTextEdit{background:rgba(255,255,255,8);border:1px solid rgba(255,255,255,18);"
            f"border-radius:8px;color:#ffffff;font-size:{self.font_ru}px;font-weight:600;padding:6px;}}"
        )

    def _on_cover_send_click(self):
        """Отправляет сообщение в общий чат — и первую вставленную вакансию,
        и любую последующую реплику (правки, уточнения, ответы на вопросы бота,
        скриншот вопросов из формы заявки)."""
        text = self.cover_chat_input.toPlainText().strip()
        if (not text and not self.cover_pending_image) or self.cover_thinking:
            return
        content = []
        if self.cover_pending_image:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": self.cover_pending_image},
            })
        content.append({
            "type": "text",
            "text": text or "See the attached screenshot — help me answer these questions.",
        })
        self.cover_messages.append({"role": "user", "content": content})
        body = ("📎 [screenshot attached]" + (f"\n{text}" if text else "")) if self.cover_pending_image else text
        self.cover_history.append(f"🧑 You:\n{body}")
        self.cover_chat_input.clear()
        self._on_cover_clear_image()
        self._send_cover_turn(self.cover_send_btn, "Thinking…", "Send", mode="analyze")

    def _on_cover_image_attached(self, qimage: QtGui.QImage):
        """Вызывается ImagePasteTextEdit при paste/drag&drop картинки —
        держим её как ожидающую отправки, не вставляем в текст."""
        self.cover_pending_image = _qimage_to_png_b64(qimage)
        self.cover_attach_label.setText("📎 Screenshot attached — will be sent with your message")
        self.cover_attach_label.setVisible(True)
        self.cover_attach_clear_btn.setVisible(True)

    def _on_cover_attach_click(self):
        """Кнопка на случай, если проще выбрать файл, чем вставлять/тащить."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Attach a screenshot", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if not path:
            return
        qimage = QtGui.QImage(path)
        if not qimage.isNull():
            self._on_cover_image_attached(qimage)

    def _on_cover_clear_image(self):
        self.cover_pending_image = None
        self.cover_attach_label.setVisible(False)
        self.cover_attach_clear_btn.setVisible(False)

    def _on_cover_save_pdf(self):
        """Сохраняет готовое письмо в PDF — через встроенный в Qt QPrinter,
        без сторонних библиотек."""
        if not self.cover_last_letter.strip():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Cover Letter as PDF", "cover_letter.pdf", "PDF Files (*.pdf)"
        )
        if not path:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path)
        doc = QtGui.QTextDocument()
        doc.setDefaultFont(QtGui.QFont("Arial", 11))
        doc.setPlainText(self.cover_last_letter)
        doc.print(printer)
        self.cover_pdf_btn.setText("✅ Saved")
        QtCore.QTimer.singleShot(1200, lambda: self.cover_pdf_btn.setText("📄 Save PDF"))

    def _on_cover_new_click(self):
        """Сбрасывает разговор целиком — для новой вакансии/задачи."""
        self.cover_messages = []
        self.cover_history = []
        self.cover_last_letter = ""
        self.cover_chat_view.clear()
        self.cover_chat_view._committed = 0
        self.cover_chat_view._live_active = False
        self.cover_chat_view._has_content = False
        self.cover_letter_output.clear()
        self.cover_generate_btn.setEnabled(False)
        self._on_cover_clear_image()

    def _on_cover_generate_click(self):
        """Пишет само письмо в отдельное поле снизу — не засоряя чат обсуждением."""
        if not self.cover_messages or self.cover_thinking:
            return
        self.cover_messages.append(
            {"role": "user", "content": "Go ahead and write the cover letter now based on our discussion."}
        )
        self.cover_history.append("🧑 You: → Generate the cover letter")
        self._send_cover_turn(
            self.cover_generate_btn, "Writing…", "✍ Generate Cover Letter", mode="write"
        )

    def _send_cover_turn(self, btn, busy_text, idle_text, mode):
        self.cover_thinking = True
        self._cover_dirty = True
        self.cover_pending_mode = mode
        self.cover_send_btn.setEnabled(False)
        self.cover_generate_btn.setEnabled(False)
        btn.setText(busy_text)
        self._btn_after_cover = (btn, idle_text)  # что вернуть кнопке по ответу
        if mode == "write":
            send_letter_async(self.cover_messages, self.cover_result_queue)
        else:
            send_analysis_async(self.cover_messages, self.cover_result_queue)

    def _mk_panel(self, header_text, is_ru, color, bold=False, flag_code=None):
        panel = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(panel)
        pv.setContentsMargins(4, 8, 4, 8)  # одинаковый отступ сверху/снизу — флаг не липнет к линиям сплиттера
        pv.setSpacing(4)

        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(5)
        # флаг — настоящая картинка, не эмодзи (Qt на Windows не умеет
        # склеивать флаг-эмодзи в символ, см. flags.py)
        flag_lbl = QtWidgets.QLabel()
        flag_lbl.setPixmap(flag_pixmap(flag_code))
        header_row.addWidget(flag_lbl)
        header = QtWidgets.QLabel(header_text)
        header.setStyleSheet("color:#6b7089;font-size:10px;font-weight:600;")
        header_row.addWidget(header)
        header_row.addStretch()

        panel._header = header      # текст — чтобы переименовать при смене языка
        panel._flag_lbl = flag_lbl  # флаг — чтобы сменить иконку при смене языка
        pv.addLayout(header_row)
        edit = QtWidgets.QTextEdit()
        edit.setReadOnly(True)
        edit.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        # запоминаем оформление, чтобы перестраивать при смене размера шрифта
        edit._color = color
        edit._bold = bold
        edit._is_ru = is_ru
        # состояние для инкрементального дописывания (см. _update_panel) —
        # без него текст пришлось бы каждый раз перерисовывать целиком,
        # из-за чего скролл «прыгал» и терялось место, где остановился
        edit._committed = 0
        edit._live_active = False
        edit._has_content = False
        edit._live_start = 0
        edit._live_sep_len = 0
        pv.addWidget(edit)
        return panel, edit

    def _apply_panel_style(self, edit):
        size = self.font_ru if edit._is_ru else self.font_en
        weight = "600" if edit._bold else "400"
        edit.setStyleSheet(
            f"QTextEdit{{background:transparent;border:none;color:{edit._color};"
            f"font-size:{size}px;font-weight:{weight};}}"
            "QScrollBar:vertical{background:transparent;width:8px;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,45);border-radius:4px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

    def _apply_fonts(self):
        for edit in (self.en_edit, self.ru_edit, self.ai_edit):
            self._apply_panel_style(edit)

    def _utility_btn_style(self):
        # общий стиль для Copy/Swap — одного размера, покрупнее прежнего
        return (
            "QPushButton{background:rgba(255,255,255,14);color:#d8dae5;"
            "border:1px solid rgba(255,255,255,28);border-radius:8px;"
            "padding:8px 18px;font-size:14px;font-weight:600;}"
            "QPushButton:hover{background:rgba(255,255,255,26);color:#ffffff;}"
        )

    def _combo_style(self):
        return (
            "QComboBox{background:rgba(255,255,255,15);color:#d8dae5;"
            "border:1px solid rgba(255,255,255,25);border-radius:6px;padding:3px 6px;font-size:11px;}"
            "QComboBox QAbstractItemView{background:#20222c;color:#d8dae5;"
            "selection-background-color:#2e7d5b;}"
        )

    def _mk_label(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("color:#9aa0b5;font-size:11px;")
        return lbl

    def _set_dot(self, state):
        """Индикатор статуса — три цвета вместо текста:
        red = остановлено/ошибка, green = работает нормально,
        yellow = что-то нештатное (грузится, отстаёт, повторяет попытку)."""
        colors = {"stopped": "#ff5c5c", "working": "#4caf7d", "busy": "#e0b23c"}
        self.dot.setStyleSheet(f"color:{colors[state]};font-size:14px;")

    def _mk_win_btn(self, symbol, hover, callback):
        btn = QtWidgets.QPushButton(symbol)
        btn.setFixedSize(22, 22)
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton{color:#9aa0b5;border:none;font-size:12px;}"
            f"QPushButton:hover{{color:{hover};}}"
        )
        btn.clicked.connect(callback)
        return btn

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
            self.max_btn.setText("▢")
        else:
            self.showMaximized()
            self.max_btn.setText("❐")

    def _open_keys_dialog(self):
        """⚙ в верхней панели — тот же диалог ключей, что и при первом запуске,
        доступен в любой момент. config.reload() внутри диалога подхватывает
        новые значения сразу, без перезапуска приложения."""
        KeysDialog(self).exec()

    def _minimize(self):
        # Qt's showMinimized() ненадёжен для окон без рамки + "поверх всех"
        # (известный баг на Windows) — сворачиваем напрямую через WinAPI
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.ShowWindow(int(self.winId()), 6)  # SW_MINIMIZE
        else:
            self.showMinimized()

    def _style_start(self):
        self.start_btn.setText("▶ Start")
        self.start_btn.setStyleSheet(
            "QPushButton{background:#2e7d5b;color:white;border:none;border-radius:8px;"
            "padding:6px 14px;font-weight:600;}QPushButton:hover{background:#379268;}"
        )

    def _style_stop(self):
        self.start_btn.setText("⏸ Stop")
        self.start_btn.setStyleSheet(
            "QPushButton{background:#8a3b3b;color:white;border:none;border-radius:8px;"
            "padding:6px 14px;font-weight:600;}QPushButton:hover{background:#a04545;}"
        )

    # Windows сам называет аудио-устройства на языке системной локали — эти
    # имена приходят из WASAPI как есть, из Python их не поменять. Подменяем
    # только типовые слова, чтобы список устройств тоже выглядел по-английски.
    _DEVICE_NAME_EN = {
        "Наушники": "Headphones",
        "Микрофон": "Microphone",
        "Динамики": "Speakers",
        "Колонки": "Speakers",
        "Гарнитура": "Headset",
    }

    def _anglicize_device_name(self, name: str) -> str:
        for ru, en in self._DEVICE_NAME_EN.items():
            name = name.replace(ru, en)
        return name

    def _fill_devices(self):
        self.device_combo.clear()
        self._device_indices = []
        try:
            for idx, name, is_default in list_loopback_devices():
                short = self._anglicize_device_name(name.replace(" [Loopback]", ""))
                self.device_combo.addItem(("★ " if is_default else "   ") + short)
                self._device_indices.append(idx)
                if is_default:
                    self.device_combo.setCurrentIndex(len(self._device_indices) - 1)
        except Exception as e:
            self.device_combo.addItem(f"Device error: {e}")
            self._device_indices.append(None)

    def _position_right(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w = int(440 * 1.35)  # шире на 35% от исходной ширины по умолчанию
        h = int(screen.height() * 0.8)
        self.resize(w, h)
        self.move(screen.right() - w - 20, screen.top() + (screen.height() - h) // 2)

    # ---------- управление ----------
    def toggle(self):
        self.stop() if self.running else self.start()

    _LANG_FLAG = {"en": "gb", "ru": "ru", "uk": "ua"}  # -> код для flags.flag_pixmap

    def _apply_client_lang_ui(self, lang_code):
        """Подстраивает панели под язык клиента: для en — перевод виден, для
        ru/uk перевод не нужен, панель клиента — единственный текстовый вывод.
        Вызывается и сразу при Старте (по выбору в настройках), и на лету —
        по факту автоопределения языка каждой фразы."""
        if lang_code == "auto":
            self.en_panel._header.setText("🌐 Client (detecting language…)")
            self.en_panel._flag_lbl.setPixmap(flag_pixmap(None))
            self.ru_panel.setVisible(True)
            self.en_check.setEnabled(True)
            self.en_panel.setVisible(self.en_check.isChecked())
            return
        translate_needed = config.CLIENT_LANGUAGES.get(lang_code, {"translate": True})["translate"]
        display_name = (self.lang_combo.itemText(self._lang_codes.index(lang_code))
                         if lang_code in self._lang_codes else lang_code)
        self.en_panel._flag_lbl.setPixmap(flag_pixmap(self._LANG_FLAG.get(lang_code)))
        self.ru_panel.setVisible(translate_needed)
        self.en_check.setEnabled(translate_needed)
        if not translate_needed:
            self.en_panel.setVisible(True)
            self.en_panel._header.setText(f"Client ({display_name})")
        else:
            self.en_panel.setVisible(self.en_check.isChecked())
            self.en_panel._header.setText("English (client)")

    def start(self):
        # замороженная сборка не тащит в себе nvidia-DLL (~1.3 ГБ) — если GPU
        # ещё не докачан, показываем модальный диалог докачки ДО того, как
        # что-либо запущено; кнопка Start в этот момент ещё не менялась, так
        # что при отмене её просто оставляем как есть (ничего откатывать не надо)
        if paths.FROZEN and config.DEVICE == "cuda" and not cuda_setup.cuda_ready():
            dlg = cuda_setup.CudaSetupDialog(self)
            if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return

        config.MODEL_SIZE = self.model_combo.currentText()
        config.WHISPER_LANG = self._lang_codes[self.lang_combo.currentIndex()]
        self._apply_client_lang_ui(config.WHISPER_LANG)

        idx = self.device_combo.currentIndex()
        device_index = self._device_indices[idx] if 0 <= idx < len(self._device_indices) else None

        self.stop_event = threading.Event()
        self.utterance_queue = queue.Queue()
        self.ai_input_queue = queue.Queue()
        self._drain(self.status_queue)
        self._drain(self.subtitle_queue)
        self._drain(self.ai_output_queue)

        self.transcriber = Transcriber(
            self.utterance_queue, self.subtitle_queue, self.stop_event, self.status_queue
        )
        self.segmenter = AudioSegmenter(
            self.utterance_queue, self.stop_event, self.status_queue, device_index
        )
        self.ai_assistant = InterviewAssistant(
            self.ai_input_queue, self.ai_output_queue, self.stop_event, self.status_queue
        )
        self.ai_assistant.start()
        self.transcriber.start()
        self.segmenter.start()

        if self.log_check.isChecked():
            log_path = self.session_log.start()
            self.log_lbl.setText("📝 " + os.path.basename(log_path))
            self.log_lbl.setToolTip(log_path)
        else:
            self.log_lbl.setText("Log disabled")
            self.log_lbl.setToolTip("")

        self.running = True
        self._last_sound_time = time.monotonic()  # отсчёт тишины начинаем с момента Start
        self._style_stop()
        self._set_dot("busy")
        self.state_lbl.setText("Starting…")
        self.device_combo.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.lang_combo.setEnabled(False)
        self.log_check.setEnabled(False)

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.session_log.close()
        self.running = False
        self._style_start()
        self._set_dot("stopped")
        self.state_lbl.setText("")
        self.level.setValue(0)
        self.device_combo.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.log_check.setEnabled(True)
        self.en_check.setEnabled(True)

    def _drain(self, q):
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _toggle_ai(self, s):
        self.ai_panel.setVisible(bool(s))
        if s and not self.ai_history:
            self._set_placeholder(self.ai_edit, "Waiting for a question — the suggestion will appear here automatically.")

    def _toggle_settings(self):
        self.settings.setVisible(not self.settings.isVisible())

    def _open_logs_folder(self):
        import subprocess
        from sessionlog import LOGS_DIR
        os.makedirs(LOGS_DIR, exist_ok=True)
        subprocess.Popen(["explorer", LOGS_DIR])

    def _on_threshold(self, val):
        config.SILENCE_RMS = val / 1000.0
        self.thr_val.setText(f"{config.SILENCE_RMS:.3f}")

    def _apply_translate_fonts(self):
        self.tr_input.setStyleSheet(
            "QTextEdit{background:rgba(255,255,255,12);border:1px solid rgba(255,255,255,25);"
            f"border-radius:8px;color:#eaeaf0;font-size:{self.font_ru}px;padding:6px;}}"
        )
        self.tr_output.setStyleSheet(
            "QTextEdit{background:rgba(255,255,255,8);border:1px solid rgba(255,255,255,18);"
            f"border-radius:8px;color:#ffffff;font-size:{self.font_ru}px;font-weight:600;padding:6px;}}"
        )

    def _on_font(self, val):
        self.font_ru = val
        self.font_en = val                # все панели одного размера
        self.font_val.setText(str(val))
        self._apply_fonts()
        self._apply_translate_fonts()
        self._apply_cover_fonts()

    # ---------- опрос очередей ----------
    def _tick(self):
        try:
            while True:
                kind, value = self.status_queue.get_nowait()
                if kind == "level":
                    self.level.setValue(min(100, int(value * 600)))
                    if value >= config.SILENCE_RMS:
                        self._last_sound_time = time.monotonic()  # реальный звук — сбрасываем таймер тишины
                elif kind == "state":
                    text = str(value)
                    self.state_lbl.setText(text)
                    # жёлтый — что-то нештатное (грузится, отстаёт, повторяет
                    # попытку), зелёный — обычная рабочая фраза, всё штатно
                    if text.startswith(("Catching up", "Hiccup", "Loading")):
                        self._set_dot("busy")
                    elif self.running:
                        self._set_dot("working")
                elif kind == "ready":
                    self._set_dot("working")
                elif kind == "device":
                    self.device_combo.setToolTip(str(value))
                elif kind == "lang":
                    # автоопределение сообщило актуальный язык клиента —
                    # подстраиваем панели на лету (актуально только для «Авто»,
                    # при явном выборе языка это просто безобидное подтверждение)
                    self._apply_client_lang_ui(str(value))
                elif kind == "error":
                    self.state_lbl.setText(str(value))
                    self._set_dot("stopped")
                elif kind == "ai_state":
                    self.ai_thinking = value == "thinking"
                    self._render_ai()
                elif kind == "ai_error":
                    self.ai_history.append(f"⚠ {value}")
                    self._render_ai()
        except queue.Empty:
            pass

        # защита от забытого включённым Live Call — если реального звука
        # (уровень выше SILENCE_RMS) не было слишком долго, останавливаем сами
        if (
            self.running
            and self._last_sound_time is not None
            and (time.monotonic() - self._last_sound_time) > config.AUTO_STOP_IDLE_MINUTES * 60
        ):
            self.stop()
            self.state_lbl.setText(
                f"Auto-stopped — no sound for {config.AUTO_STOP_IDLE_MINUTES:g} min (saving API costs)"
            )
            return

        ru_changed = en_changed = False
        try:
            while True:
                kind, en, ru, lang = self.subtitle_queue.get_nowait()
                if kind == "final":
                    self.en_history.append(en)
                    self.ru_history.append(ru)
                    self.en_history = self.en_history[-HISTORY_MAX:]
                    self.ru_history = self.ru_history[-HISTORY_MAX:]
                    self.live_en = ""
                    self.live_ru = ""
                    self.session_log.write(en, ru)
                    if self.ai_check.isChecked() and self.ai_input_queue is not None:
                        self.ai_input_queue.put((en, lang))
                    ru_changed = en_changed = True
                else:                             # interim — живой английский + (иногда) русский
                    self.live_en = en
                    en_changed = True
                    if ru:
                        self.live_ru = ru
                        ru_changed = True
        except queue.Empty:
            pass

        if en_changed and self.en_check.isChecked():
            self._update_panel(self.en_edit, self.en_history, self.live_en, sep=" ")
        if ru_changed:
            self._update_panel(self.ru_edit, self.ru_history, self.live_ru, sep=" ")

        ai_changed = False
        try:
            while True:
                answer = self.ai_output_queue.get_nowait()
                self.ai_history.append(answer)
                self.ai_history = self.ai_history[-50:]
                ai_changed = True
        except queue.Empty:
            pass
        if ai_changed:
            self._render_ai()

        try:
            while True:
                status, text, source_lang = self.translate_result_queue.get_nowait()
                self.tr_output.setPlainText(text if status == "ok" else f"⚠ {text}")
                if status == "ok":
                    self._tr_last_source = source_lang  # для «Swap» — знаем, куда назад
                self.tr_btn.setEnabled(True)
                self.tr_btn.setText("Translate  (Ctrl+Enter)")
        except queue.Empty:
            pass

        try:
            while True:
                status, text, raw = self.cover_result_queue.get_nowait()
                if raw:
                    self.cover_messages.append({"role": "assistant", "content": raw})
                if status == "ok" and self.cover_pending_mode == "write":
                    # письмо идёт в отдельное поле снизу, не в чат — короткая
                    # пометка в чате просто фиксирует, что оно готово
                    self.cover_letter_output.setPlainText(text)
                    self.cover_last_letter = text
                    self.cover_history.append("🤖 Bot: ✍ Letter written below ↓")
                elif status == "ok":
                    self.cover_history.append(f"🤖 Bot:\n{text}")
                elif status == "not_fit":
                    self.cover_history.append(f"🤖 Bot: ⚠ Not a good fit — {text}")
                else:
                    self.cover_history.append(f"🤖 Bot: ⚠ {text}")
                self.cover_thinking = False
                self._cover_dirty = True
                self.cover_send_btn.setEnabled(True)
                self.cover_generate_btn.setEnabled(True)
                if self._btn_after_cover:
                    btn, idle_text = self._btn_after_cover
                    btn.setText(idle_text)
                    self._btn_after_cover = None
        except queue.Empty:
            pass

        if self._cover_dirty:
            cover_live = "⏳ thinking…" if self.cover_thinking else ""
            self._update_panel(self.cover_chat_view, self.cover_history, cover_live,
                                sep="\n\n— — —\n\n")
            self._cover_dirty = False

    def _render_ai(self):
        live = "⏳ thinking of an answer…" if self.ai_thinking else ""
        self._update_panel(self.ai_edit, self.ai_history, live, sep="\n\n— — —\n\n")

    def _set_placeholder(self, edit, text):
        """Показывает служебный текст (не часть истории), который аккуратно
        уступит место реальному содержимому при первых данных."""
        edit.clear()
        edit.setPlainText(text)
        edit._committed = 0
        edit._live_active = False
        edit._has_content = False

    def _update_panel(self, edit, history, live_text, sep="\n"):
        """Дописывает новые записи и обновляет черновую строку в конце панели,
        не трогая уже показанный текст выше — иначе скролл прыгает и читатель
        теряет место, на котором остановился."""
        # если список истории обрезался по HISTORY_MAX — редкий случай, полная
        # переотрисовка неизбежна (раз в HISTORY_MAX реплик, не на каждый тик)
        if len(history) < edit._committed:
            edit.clear()
            edit._committed = 0
            edit._live_active = False
            edit._has_content = False

        if not edit._has_content and (history or live_text):
            edit.clear()
            edit._committed = 0
            edit._live_active = False
            edit._has_content = True

        bar = edit.verticalScrollBar()
        was_at_bottom = bar.value() >= bar.maximum() - 4

        if len(history) > edit._committed:
            if edit._live_active:
                self._remove_live(edit)
                edit._live_active = False
            for item in history[edit._committed:]:
                self._append_committed(edit, item, sep)
            edit._committed = len(history)

        if live_text:
            if edit._live_active:
                self._replace_live(edit, live_text)
            else:
                self._start_live(edit, live_text, sep)
        elif edit._live_active:
            self._remove_live(edit)
            edit._live_active = False

        if was_at_bottom:
            bar.setValue(bar.maximum())

    def _append_committed(self, edit, text, sep):
        cursor = edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        if not edit.document().isEmpty():
            cursor.insertText(sep)
        cursor.insertText(text)

    def _start_live(self, edit, text, sep):
        """Начинает черновую строку и запоминает её позицию (символом, а не
        абзацем — sep может быть и пробелом, без переноса строки)."""
        cursor = edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        sep_len = 0
        if not edit.document().isEmpty():
            cursor.insertText(sep)
            sep_len = len(sep)
        edit._live_start = cursor.position()
        edit._live_sep_len = sep_len
        cursor.insertText(text)
        edit._live_active = True

    def _replace_live(self, edit, text):
        cursor = edit.textCursor()
        cursor.setPosition(edit._live_start)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End,
                             QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)

    def _remove_live(self, edit):
        cursor = edit.textCursor()
        start = max(0, edit._live_start - edit._live_sep_len)
        cursor.setPosition(start)
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End,
                             QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

    # ---------- перетаскивание/закрытие ----------
    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.MouseButton.LeftButton and not self.isMaximized():
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and not self.isMaximized():
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, e):
        if self.stop_event:
            self.stop_event.set()
        self.session_log.close()
        super().closeEvent(e)


def run():
    import sys
    # без этого Windows группирует Python-приложения под общей иконкой
    # python.exe в таскбаре вместо иконки самого приложения — нужно выставить
    # ДО создания главного окна. run() — общая точка входа и для main.py,
    # и для прямого запуска app.py, так что один вызов здесь покрывает оба пути.
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MakeFlows.RealtimeTranslator"
            )
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    win = TranslatorApp()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
