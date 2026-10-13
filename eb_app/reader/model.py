"""
model.py
========
Layout model shared by native-PDF text and OCR output. Everything downstream (field extraction)
works on Cells with bounding boxes, so a bill is read the same way whether it was a digital PDF,
a scanned PDF or a phone photo.
"""

from dataclasses import dataclass, field


@dataclass
class Cell:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0          # 0..1; native text is 1.0, OCR is the engine's confidence
    src: str = "native"        # native | ocr
    page: int = 1
    anchor: object = None      # for a virtual (merged two-line) label: the lower real cell it is anchored to

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1e-6)

    @property
    def w(self) -> float:
        return max(self.x1 - self.x0, 1e-6)

    def to_json(self) -> dict:
        return {"t": self.text, "b": [round(self.x0, 1), round(self.y0, 1), round(self.x1, 1), round(self.y1, 1)],
                "c": round(self.conf, 3), "s": self.src}

    @classmethod
    def from_json(cls, d: dict, page: int) -> "Cell":
        b = d["b"]
        return cls(d["t"], b[0], b[1], b[2], b[3], d.get("c", 1.0), d.get("s", "ocr"), page)


@dataclass
class Page:
    number: int
    width: float
    height: float
    cells: list = field(default_factory=list)
    method: str = "native"              # native | ocr | none
    ocr_note: str = ""
    rotation_applied: int = 0           # degrees the page had to be turned to read it
    angle_spread: float = 0.0           # spread of text-line angles (curved / perspective photo => high)
    mean_conf: float = 1.0

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in sorted(self.cells, key=lambda c: (round(c.yc / max(c.h, 1)), c.x0)))


@dataclass
class Document:
    path: str
    kind: str                           # pdf | image | unsupported
    pages: list = field(default_factory=list)
    sha256: str = ""
    warnings: list = field(default_factory=list)
    scripts: dict = field(default_factory=dict)      # detected writing systems -> character count

    @property
    def all_cells(self):
        for p in self.pages:
            yield from p.cells
