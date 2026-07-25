"""Настоящие флаги как картинки, а не эмодзи.

Qt на Windows рисует текст через свой движок (HarfBuzz), а не через нативный
DirectWrite — поэтому флаг-эмодзи (два "regional indicator" символа) не
склеиваются в картинку флага и остаются буквами (GB/RU). Это ограничение самого
Qt, не шрифта — так что рисуем флаги сами через QPainter и вставляем как иконки.
"""
from PyQt6 import QtCore, QtGui


def _flag_ru(w, h):
    pm = QtGui.QPixmap(w, h)
    p = QtGui.QPainter(pm)
    third = h / 3
    p.fillRect(QtCore.QRectF(0, 0, w, third), QtGui.QColor("#ffffff"))
    p.fillRect(QtCore.QRectF(0, third, w, third), QtGui.QColor("#0039a6"))
    p.fillRect(QtCore.QRectF(0, 2 * third, w, h - 2 * third), QtGui.QColor("#d52b1e"))
    p.end()
    return pm


def _flag_ua(w, h):
    pm = QtGui.QPixmap(w, h)
    p = QtGui.QPainter(pm)
    half = h / 2
    p.fillRect(QtCore.QRectF(0, 0, w, half), QtGui.QColor("#0057b7"))
    p.fillRect(QtCore.QRectF(0, half, w, h - half), QtGui.QColor("#ffd700"))
    p.end()
    return pm


def _flag_gb(w, h):
    pm = QtGui.QPixmap(w, h)
    pm.fill(QtGui.QColor("#012169"))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

    # белые диагонали — Андреевский крест
    pen = QtGui.QPen(QtGui.QColor("#ffffff"))
    pen.setWidthF(h * 0.32)
    p.setPen(pen)
    p.drawLine(QtCore.QPointF(0, 0), QtCore.QPointF(w, h))
    p.drawLine(QtCore.QPointF(w, 0), QtCore.QPointF(0, h))

    # красные диагонали — крест святого Патрика, тоньше, поверх белых
    pen.setColor(QtGui.QColor("#c8102e"))
    pen.setWidthF(h * 0.12)
    p.setPen(pen)
    p.drawLine(QtCore.QPointF(0, 0), QtCore.QPointF(w, h))
    p.drawLine(QtCore.QPointF(w, 0), QtCore.QPointF(0, h))

    # белый прямой крест — крест святого Георгия, фон
    p.fillRect(QtCore.QRectF(0, h * 0.36, w, h * 0.28), QtGui.QColor("#ffffff"))
    p.fillRect(QtCore.QRectF(w * 0.40, 0, w * 0.20, h), QtGui.QColor("#ffffff"))

    # красный прямой крест поверх
    p.fillRect(QtCore.QRectF(0, h * 0.42, w, h * 0.16), QtGui.QColor("#c8102e"))
    p.fillRect(QtCore.QRectF(w * 0.44, 0, w * 0.12, h), QtGui.QColor("#c8102e"))

    p.end()
    return pm


_BUILDERS = {"gb": _flag_gb, "ru": _flag_ru, "ua": _flag_ua}


def flag_pixmap(code: str, w: int = 20, h: int = 14) -> QtGui.QPixmap:
    """Растровая иконка флага. code: gb / ru / ua. Неизвестный код — пустая
    (прозрачная) картинка того же размера, чтобы layout не съезжал."""
    builder = _BUILDERS.get(code)
    if builder is None:
        pm = QtGui.QPixmap(w, h)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        return pm
    return builder(w, h)
