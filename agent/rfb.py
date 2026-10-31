"""Cliente VNC (protocolo RFB 3.3/3.7/3.8) propio, sobre websocket (websockify).

Solo lo necesario para operar un escritorio como una persona:
- autenticación VNC (DES con bits invertidos, como exige el protocolo)
- framebuffer en encoding Raw + CopyRect + DesktopSize
- eventos de puntero (clic con pulsación >= 100 ms, porque el escritorio ignora clics más cortos)
- eventos de teclado con keysyms Unicode (acentos y ñ) y combinaciones (Ctrl+A)
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Optional

import numpy as np
from PIL import Image

from . import config

log = logging.getLogger("ghost.rfb")

KEYSYMS = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "esc": 0xFF1B,
    "backspace": 0xFF08, "delete": 0xFFFF, "home": 0xFF50, "end": 0xFF57,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pageup": 0xFF55, "pagedown": 0xFF56, "ctrl": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9,
    "space": 0x0020, "f5": 0xFFC2,
}


def char_keysym(c: str) -> int:
    o = ord(c)
    if c == "\n":
        return KEYSYMS["enter"]
    if c == "\t":
        return KEYSYMS["tab"]
    if 0x20 <= o <= 0x7E or 0xA0 <= o <= 0xFF:
        return o  # Latin-1 coincide con su keysym (á=0xE1, ñ=0xF1, ...)
    return 0x01000000 | o  # keysym Unicode genérico


def _vnc_key(password: str) -> bytes:
    pw = (password or "").encode("latin-1", "replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in pw)  # cada byte con sus bits invertidos


class RFBError(Exception):
    pass


class RFBClient:
    def __init__(self, url: str, password: str):
        self.url = url
        self.password = password
        self.ws = None
        self._buf = bytearray()
        self.width = 0
        self.height = 0
        self.fb: Optional[np.ndarray] = None  # H x W x 4 (B, G, R, X)
        self._reader: Optional[asyncio.Task] = None
        self._updated = asyncio.Event()
        self.updates = 0
        self.closed = False
        self._mouse = (0, 0)

    # ------------------------------------------------------------ transporte
    async def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            msg = await self.ws.recv()
            if isinstance(msg, str):
                msg = msg.encode("latin-1")
            self._buf.extend(msg)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _send(self, data: bytes) -> None:
        await self.ws.send(data)

    # ------------------------------------------------------------ conexión y handshake
    async def connect(self) -> None:
        import websockets

        self.ws = await websockets.connect(
            self.url, subprotocols=["binary"], max_size=None, open_timeout=15, ping_interval=20,
        )
        ver = await self._recv_exact(12)
        if not ver.startswith(b"RFB "):
            raise RFBError(f"respuesta inesperada del servidor VNC: {ver!r}")
        major, minor = int(ver[4:7]), int(ver[8:11])
        minor = 8 if (major, minor) >= (3, 8) else (7 if (major, minor) >= (3, 7) else 3)
        await self._send(f"RFB 003.{minor:03d}\n".encode())

        if minor == 3:
            (sec,) = struct.unpack(">I", await self._recv_exact(4))
            if sec == 0:
                raise RFBError(await self._reason())
        else:
            (n,) = struct.unpack(">B", await self._recv_exact(1))
            if n == 0:
                raise RFBError(await self._reason())
            types = list(await self._recv_exact(n))
            if 2 in types and (self.password or 1 not in types):
                sec = 2
            elif 1 in types:
                sec = 1
            else:
                raise RFBError(f"tipos de seguridad no soportados: {types}")
            await self._send(bytes([sec]))

        if sec == 2:
            from Crypto.Cipher import DES

            challenge = await self._recv_exact(16)
            await self._send(DES.new(_vnc_key(self.password), DES.MODE_ECB).encrypt(challenge))
        if sec == 2 or minor == 8:
            (res,) = struct.unpack(">I", await self._recv_exact(4))
            if res != 0:
                raise RFBError("autenticación VNC rechazada" + (f": {await self._reason()}" if minor == 8 else ""))

        await self._send(b"\x01")  # ClientInit: compartido
        w, h = struct.unpack(">HH", await self._recv_exact(4))
        await self._recv_exact(16)  # formato de pixel del servidor (lo reemplazamos)
        (nlen,) = struct.unpack(">I", await self._recv_exact(4))
        await self._recv_exact(nlen)
        self._resize(w, h)

        # SetPixelFormat: 32 bpp, true color, little endian -> bytes B,G,R,X
        pf = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await self._send(struct.pack(">B3x", 0) + pf)
        encs = [0, 1, -223]  # Raw, CopyRect, DesktopSize
        await self._send(struct.pack(">BxH", 2, len(encs)) + b"".join(struct.pack(">i", e) for e in encs))

        self._reader = asyncio.create_task(self._read_loop())
        await self.request(incremental=False)
        await self.wait_update(timeout=5)

    async def _reason(self) -> str:
        (n,) = struct.unpack(">I", await self._recv_exact(4))
        return (await self._recv_exact(n)).decode("utf-8", "replace")

    def _resize(self, w: int, h: int) -> None:
        self.width, self.height = w, h
        self.fb = np.zeros((h, w, 4), dtype=np.uint8)

    async def close(self) -> None:
        self.closed = True
        if self._reader:
            self._reader.cancel()
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------ mensajes del servidor
    async def _read_loop(self) -> None:
        try:
            while True:
                (mtype,) = struct.unpack(">B", await self._recv_exact(1))
                if mtype == 0:
                    await self._recv_exact(1)
                    (nrects,) = struct.unpack(">H", await self._recv_exact(2))
                    for _ in range(nrects):
                        x, y, w, h, enc = struct.unpack(">HHHHi", await self._recv_exact(12))
                        if enc == 0:
                            data = await self._recv_exact(w * h * 4)
                            if w and h:
                                self.fb[y:y + h, x:x + w] = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
                        elif enc == 1:
                            sx, sy = struct.unpack(">HH", await self._recv_exact(4))
                            self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
                        elif enc == -223:
                            self._resize(w, h)
                        else:
                            raise RFBError(f"encoding no solicitado: {enc}")
                    self.updates += 1
                    self._updated.set()
                elif mtype == 1:
                    await self._recv_exact(1)
                    _, n = struct.unpack(">HH", await self._recv_exact(4))
                    await self._recv_exact(n * 6)
                elif mtype == 2:
                    pass
                elif mtype == 3:
                    await self._recv_exact(3)
                    (n,) = struct.unpack(">I", await self._recv_exact(4))
                    await self._recv_exact(n)
                else:
                    raise RFBError(f"mensaje desconocido del servidor: {mtype}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self.closed:
                log.warning("lectura VNC terminó: %s", e)
            self.closed = True
            self._updated.set()

    # ------------------------------------------------------------ framebuffer
    async def request(self, incremental: bool = True) -> None:
        await self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    async def wait_update(self, timeout: float = 1.0) -> bool:
        self._updated.clear()
        try:
            await asyncio.wait_for(self._updated.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def image(self) -> Image.Image:
        if self.closed and self.fb is None:
            raise RFBError("conexión VNC cerrada")
        return Image.fromarray(self.fb[:, :, [2, 1, 0]].copy(), "RGB")

    def signature(self) -> bytes:
        """Huella barata del frame actual para detectar si la pantalla dejó de moverse."""
        small = self.fb[::8, ::8, :3]
        return small.tobytes()

    async def capture_stable(self) -> Image.Image:
        """Espera hasta que 2 frames seguidos sean iguales (tope configurable). Nunca devuelve una pantalla vieja
        inmediatamente después de una acción: siempre espera al menos STABLE_MIN_WAIT_S."""
        if self.closed:
            raise RFBError("conexión VNC cerrada")
        await asyncio.sleep(config.STABLE_MIN_WAIT_S)
        t0 = time.time()
        prev = None
        while True:
            await self.request(incremental=True)
            await self.wait_update(timeout=config.STABLE_POLL_S)
            sig = self.signature()
            if prev is not None and sig == prev:
                break
            prev = sig
            if time.time() - t0 > config.STABLE_TIMEOUT_S:
                break
            await asyncio.sleep(config.STABLE_POLL_S / 2)
        if self.closed:
            raise RFBError("conexión VNC cerrada")
        return self.image()

    # ------------------------------------------------------------ entrada
    async def _pointer(self, x: int, y: int, mask: int) -> None:
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        self._mouse = (x, y)
        await self._send(struct.pack(">BBHH", 5, mask, x, y))

    async def move(self, x: int, y: int) -> None:
        await self._pointer(x, y, 0)

    async def click(self, x: int, y: int, button: int = 1) -> None:
        mask = {1: 1, 2: 2, 3: 4}[button]
        await self._pointer(x, y, 0)
        await asyncio.sleep(0.05)
        await self._pointer(x, y, mask)
        await asyncio.sleep(config.CLICK_HOLD_S)  # > 100 ms o el escritorio ignora el clic
        await self._pointer(x, y, 0)

    async def double_click(self, x: int, y: int) -> None:
        await self.click(x, y)
        await asyncio.sleep(0.08)
        await self.click(x, y)

    async def scroll(self, x: int, y: int, down: bool = True, clicks: int = 3) -> None:
        mask = 16 if down else 8
        for _ in range(clicks):
            await self._pointer(x, y, mask)
            await asyncio.sleep(0.03)
            await self._pointer(x, y, 0)
            await asyncio.sleep(0.05)

    async def key_event(self, keysym: int, down: bool) -> None:
        await self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    async def key(self, name: str) -> None:
        """Tecla o combinación: 'enter', 'tab', 'ctrl+a', 'shift+tab'."""
        parts = [p.strip().lower() for p in name.split("+") if p.strip()]
        syms = [KEYSYMS.get(p, char_keysym(p) if len(p) == 1 else None) for p in parts]
        if any(s is None for s in syms):
            raise ValueError(f"tecla desconocida: {name}")
        for s in syms:
            await self.key_event(s, True)
            await asyncio.sleep(config.KEY_DELAY_S)
        for s in reversed(syms):
            await self.key_event(s, False)
            await asyncio.sleep(config.KEY_DELAY_S)

    async def type_text(self, text: str) -> None:
        for c in text:
            s = char_keysym(c)
            await self.key_event(s, True)
            await asyncio.sleep(config.KEY_DELAY_S)
            await self.key_event(s, False)
            await asyncio.sleep(config.KEY_DELAY_S)

    async def replace_field(self, x: int, y: int, text: str) -> None:
        """Escribir sobre un campo con texto concatena: clic, Ctrl+A, borrar y escribir."""
        await self.click(x, y)
        await asyncio.sleep(0.15)
        await self.key("ctrl+a")
        await self.key("backspace")
        await self.type_text(text)
