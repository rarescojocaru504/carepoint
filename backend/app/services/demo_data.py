"""Demo dataset for local development, CI and pitch demos.

Machines sit at real (approximate) locations around university campuses in
Bucharest. To demo elsewhere, edit MACHINES; everything else adapts.

Deliberately no medicines: in most of the EU, over-the-counter drugs cannot be
sold from unattended machines without pharmacy oversight.
"""

from dataclasses import dataclass

from app.models.enums import MachineStatus, ProductCategory, UserRole


@dataclass(frozen=True)
class DemoUser:
    email: str
    full_name: str
    role: UserRole


@dataclass(frozen=True)
class DemoProduct:
    sku: str
    name: str
    category: ProductCategory
    price_cents: int
    description: str


@dataclass(frozen=True)
class DemoSlot:
    slot_code: str
    sku: str | None  # None = empty slot
    capacity: int
    initial_quantity: int
    low_stock_threshold: int = 2


@dataclass(frozen=True)
class DemoMachine:
    serial_number: str
    name: str
    address: str
    lat: float
    lng: float
    slots: tuple[DemoSlot, ...]
    status: MachineStatus = MachineStatus.ACTIVE


USERS: tuple[DemoUser, ...] = (
    DemoUser("admin@carepoint.demo", "Demo Admin", UserRole.ADMIN),
    DemoUser("operator@carepoint.demo", "Demo Operator", UserRole.OPERATOR),
    DemoUser("customer@carepoint.demo", "Demo Customer", UserRole.CUSTOMER),
)

PRODUCTS: tuple[DemoProduct, ...] = (
    DemoProduct("PAD-REG-10", "Sanitary pads, regular (10)", ProductCategory.MENSTRUAL_CARE,
                1299, "Ultra-thin pads with wings."),
    DemoProduct("TAMP-REG-16", "Tampons, regular (16)", ProductCategory.MENSTRUAL_CARE,
                1999, "Applicator tampons, regular absorbency."),
    DemoProduct("CONDOM-3", "Condoms (3)", ProductCategory.SEXUAL_HEALTH,
                1999, "Latex condoms, CE certified."),
    DemoProduct("PREG-TEST-1", "Pregnancy test", ProductCategory.SEXUAL_HEALTH,
                2499, "Early-detection test, results in 3 minutes."),
    DemoProduct("PLASTER-20", "Plasters, assorted (20)", ProductCategory.FIRST_AID,
                999, "Waterproof plasters in assorted sizes."),
    DemoProduct("MASK-FFP2-5", "FFP2 masks (5)", ProductCategory.FIRST_AID,
                1499, "Individually wrapped FFP2 masks."),
    DemoProduct("SANIT-50ML", "Hand sanitiser 50 ml", ProductCategory.HYGIENE,
                899, "70% alcohol gel."),
    DemoProduct("TOOTH-KIT", "Travel toothbrush kit", ProductCategory.HYGIENE,
                1499, "Folding toothbrush with travel-size toothpaste."),
    DemoProduct("DEO-50ML", "Deodorant roll-on 50 ml", ProductCategory.HYGIENE,
                1599, "Unscented, 48h."),
    DemoProduct("DIAPER-4-5", "Diapers size 4 (5)", ProductCategory.BABY_CARE,
                1999, "Emergency pack of five."),
    DemoProduct("WIPES-20", "Wet wipes (20)", ProductCategory.ESSENTIALS,
                699, "Alcohol-free, sensitive skin."),
    DemoProduct("CABLE-USBC", "USB-C charging cable 1 m", ProductCategory.ESSENTIALS,
                2999, "USB-A to USB-C, fast charge."),
)  # fmt: skip


def _standard_layout(stock: tuple[int, ...]) -> tuple[DemoSlot, ...]:
    """Eight slots (A1-A4, B1-B4) with the core range; `stock` sets fill levels."""
    skus = ("PAD-REG-10", "TAMP-REG-16", "CONDOM-3", "PREG-TEST-1",
            "PLASTER-20", "SANIT-50ML", "WIPES-20", "CABLE-USBC")  # fmt: skip
    codes = ("A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4")
    return tuple(
        DemoSlot(code, sku, capacity=10, initial_quantity=qty)
        for code, sku, qty in zip(codes, skus, stock, strict=True)
    )


MACHINES: tuple[DemoMachine, ...] = (
    DemoMachine("CP-BUC-0001", "Universitate - Piata Universitatii",
                "Bd. Regina Elisabeta 4-12, Bucuresti", 44.4355, 26.1013,
                _standard_layout((8, 6, 9, 4, 10, 7, 5, 3))),
    DemoMachine("CP-BUC-0002", "Politehnica - Rectorat",
                "Splaiul Independentei 313, Bucuresti", 44.4386, 26.0496,
                _standard_layout((10, 10, 8, 6, 9, 10, 10, 6))),
    DemoMachine("CP-BUC-0003", "Campus Regie - Camin C1",
                "Str. Mihail Moxa / Regie, Bucuresti", 44.4430, 26.0640,
                _standard_layout((2, 1, 0, 3, 6, 5, 4, 2))),  # low stock + one sold out
    DemoMachine("CP-BUC-0004", "ASE - Piata Romana",
                "Piata Romana 6, Bucuresti", 44.4475, 26.0973,
                _standard_layout((7, 5, 6, 5, 8, 6, 9, 4))),
    DemoMachine("CP-BUC-0005", "Gara de Nord - Hol principal",
                "Piata Garii de Nord 1, Bucuresti", 44.4467, 26.0752,
                (DemoSlot("A1", "PAD-REG-10", 12, 10), DemoSlot("A2", "TOOTH-KIT", 8, 6),
                 DemoSlot("A3", "DEO-50ML", 8, 5), DemoSlot("A4", "MASK-FFP2-5", 10, 9),
                 DemoSlot("B1", "DIAPER-4-5", 6, 4), DemoSlot("B2", "WIPES-20", 12, 11),
                 DemoSlot("B3", "CABLE-USBC", 6, 2), DemoSlot("B4", None, 10, 0))),
    DemoMachine("CP-BUC-0006", "Medicina - UMF Carol Davila",
                "Bd. Eroii Sanitari 8, Bucuresti", 44.4350, 26.0700,
                _standard_layout((5, 5, 5, 5, 5, 5, 5, 5)),
                status=MachineStatus.MAINTENANCE),
)  # fmt: skip
