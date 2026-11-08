"""IDL-driven Anchor event decoder (borsh).

pump.fun often adds new fields to the END of events, so the decoder tolerates prefixes: it
decodes while bytes remain. If they run out, the other fields are absent (old transaction).
Leftover bytes are reported as `_extra_bytes` (the program is newer than the IDL).
"""
import base64
import json
import struct

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


class _Short(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise _Short
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    @property
    def left(self) -> int:
        return len(self.d) - self.i


_INT = {"u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I",
        "i32": "<i", "u64": "<Q", "i64": "<q"}


class IdlEvents:
    def __init__(self, path: str, label: str):
        idl = json.load(open(path, encoding="utf-8"))
        self.label = label
        self.address = idl["address"]
        self.types = {t["name"]: t["type"] for t in idl.get("types", [])}
        self.by_disc = {bytes(e["discriminator"]): e["name"] for e in idl.get("events", [])}

    def _val(self, r: _Reader, t):
        if isinstance(t, str):
            if t in _INT:
                return struct.unpack(_INT[t], r.take(struct.calcsize(_INT[t])))[0]
            if t == "bool":
                return r.take(1) != b"\0"
            if t == "pubkey":
                return b58encode(r.take(32))
            if t in ("u128", "i128"):
                return int.from_bytes(r.take(16), "little", signed=t == "i128")
            if t == "string":
                n = struct.unpack("<I", r.take(4))[0]
                return r.take(n).decode("utf-8", "replace")
            if t == "bytes":
                n = struct.unpack("<I", r.take(4))[0]
                return r.take(n).hex()
            raise ValueError(f"unknown type: {t}")
        if "vec" in t:
            n = struct.unpack("<I", r.take(4))[0]
            return [self._val(r, t["vec"]) for _ in range(n)]
        if "option" in t:
            return self._val(r, t["option"]) if r.take(1) != b"\0" else None
        if "array" in t:
            inner, n = t["array"]
            return [self._val(r, inner) for _ in range(n)]
        if "defined" in t:
            name = t["defined"]["name"] if isinstance(t["defined"], dict) else t["defined"]
            return self._defined(r, self.types[name])
        raise ValueError(f"unknown type: {t}")

    def _defined(self, r: _Reader, td):
        if td["kind"] == "struct":
            return {f["name"]: self._val(r, f["type"]) for f in td.get("fields", [])}
        if td["kind"] == "enum":
            idx = r.take(1)[0]
            v = td["variants"][idx]
            fields = v.get("fields")
            if not fields:
                return v["name"]
            if isinstance(fields[0], dict):
                return {v["name"]: {f["name"]: self._val(r, f["type"]) for f in fields}}
            return {v["name"]: [self._val(r, f) for f in fields]}
        raise ValueError(td["kind"])

    def decode(self, data: bytes):
        """-> (event name, fields), or None if the event is not from this program."""
        name = self.by_disc.get(data[:8])
        if name is None:
            return None
        r = _Reader(data[8:])
        out = {}
        missing = []
        fields = self.types[name].get("fields", [])
        for k, f in enumerate(fields):
            try:
                out[f["name"]] = self._val(r, f["type"])
            except _Short:
                missing = [x["name"] for x in fields[k:]]
                break
        if missing:
            out["_missing"] = missing
        if r.left:
            out["_extra_bytes"] = r.left
            out["_extra_hex"] = r.d[r.i:].hex()
        return name, out

    def decode_b64(self, s: str):
        try:
            return self.decode(base64.b64decode(s))
        except Exception:
            return None
