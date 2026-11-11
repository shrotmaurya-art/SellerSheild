import sys
from pathlib import Path

# Add store-api to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_API_DIR = REPO_ROOT / "store-api"
if str(STORE_API_DIR) not in sys.path:
    sys.path.insert(0, str(STORE_API_DIR))

from db import engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Order,
    OrderItem,
    Product,
    Refund,
    Return,
    ReturnItem,
    Shipment,
    ShipmentEvent,
    User,
)


def verify_seed() -> int:
    results: list[tuple[str, bool, str]] = []

    with Session(engine) as session:
        # SC-01: ORD-1048, C001, in transit, ETA in future, total 89900 paise
        ord_1048 = session.get(Order, "ORD-1048")
        shp_1048 = session.get(Shipment, "SHP-1048")
        sc01_pass = (
            ord_1048 is not None
            and ord_1048.customer_id == "C001"
            and ord_1048.total_paise == 89900
            and shp_1048 is not None
            and shp_1048.status == "in_transit"
        )
        results.append(("SC-01", sc01_pass, "ORD-1048 C001 in transit total 89900 paise"))

        # SC-02: ORD-1042 requested by C002 -> owned by C001
        ord_1042 = session.get(Order, "ORD-1042")
        c002 = session.get(Customer, "C002")
        sc02_pass = (
            ord_1042 is not None
            and ord_1042.customer_id == "C001"
            and c002 is not None
            and ord_1042.customer_id != "C002"
        )
        results.append(("SC-02", sc02_pass, "ORD-1042 owned by C001, distinct from C002"))

        # SC-03: C005 customer exists for token-identity chat testing
        c005 = session.get(Customer, "C005")
        u005 = session.get(User, "C005")
        sc03_pass = c005 is not None and u005 is not None and u005.role == "customer"
        results.append(("SC-03", sc03_pass, "C005 user and customer record exists"))

        # SC-04: ORD-1042, C001, delivered T0-3d, ₹1,200 (> ₹500 limit)
        sc04_pass = (
            ord_1042 is not None
            and ord_1042.customer_id == "C001"
            and ord_1042.status == "delivered"
            and ord_1042.total_paise == 120000
            and ord_1042.delivered_at is not None
        )
        results.append(("SC-04", sc04_pass, "ORD-1042 delivered ₹1,200 (>₹500 limit)"))

        # SC-05: ORD-2032, C002, delivered T0-2d, ₹499 (< ₹500 limit)
        ord_2032 = session.get(Order, "ORD-2032")
        sc05_pass = (
            ord_2032 is not None
            and ord_2032.customer_id == "C002"
            and ord_2032.status == "delivered"
            and ord_2032.total_paise == 49900
        )
        results.append(("SC-05", sc05_pass, "ORD-2032 delivered ₹499 (<₹500 auto-approved)"))

        # SC-06: ORD-1051, C001, delivered T0-16d, ₹799 (outside 7d window)
        ord_1051 = session.get(Order, "ORD-1051")
        sc06_pass = (
            ord_1051 is not None
            and ord_1051.customer_id == "C001"
            and ord_1051.status == "delivered"
            and ord_1051.total_paise == 79900
            and ord_1051.delivered_at is not None
        )
        results.append(("SC-06", sc06_pass, "ORD-1051 delivered T0-16d (outside return window)"))

        # SC-07: ORD-1060, C001, face serum ₹550, hygiene_sensitive = True
        ord_1060 = session.get(Order, "ORD-1060")
        item_1060 = session.get(OrderItem, "ITEM-1060-1")
        prod_301 = session.get(Product, "P-301")
        sc07_pass = (
            ord_1060 is not None
            and ord_1060.customer_id == "C001"
            and item_1060 is not None
            and item_1060.sku == "P-301"
            and prod_301 is not None
            and prod_301.hygiene_sensitive is True
        )
        results.append(("SC-07", sc07_pass, "ORD-1060 hygiene-sensitive product P-301"))

        # SC-08: ORD-2031, C002, 2 items, coupon ₹100, total ₹1,647 with largest-remainder allocation
        ord_2031 = session.get(Order, "ORD-2031")
        item_2031_1 = session.get(OrderItem, "ITEM-2031-1")
        item_2031_2 = session.get(OrderItem, "ITEM-2031-2")
        sc08_pass = (
            ord_2031 is not None
            and ord_2031.customer_id == "C002"
            and ord_2031.total_paise == 164700
            and ord_2031.discount_paise == 10000
            and item_2031_1 is not None
            and item_2031_2 is not None
            and (item_2031_1.discount_alloc_paise + item_2031_2.discount_alloc_paise) == 10000
            and item_2031_1.discount_alloc_paise == 8002
            and item_2031_2.discount_alloc_paise == 1998
            and (item_2031_1.line_paid_paise + item_2031_2.line_paid_paise) == 164700
        )
        results.append(("SC-08", sc08_pass, "ORD-2031 largest-remainder discount allocation (8002 + 1998 = 10000)"))

        # SC-09: ORD-3050, C003, RET-9050 active return already requested
        ord_3050 = session.get(Order, "ORD-3050")
        ret_9050 = session.get(Return, "RET-9050")
        ret_item_9050 = session.scalar(
            select(ReturnItem).where(ReturnItem.return_id == "RET-9050", ReturnItem.item_id == "ITEM-3050-1")
        )
        sc09_pass = (
            ord_3050 is not None
            and ord_3050.customer_id == "C003"
            and ret_9050 is not None
            and ret_9050.status == "requested"
            and ret_item_9050 is not None
            and ret_item_9050.status == "requested"
        )
        results.append(("SC-09", sc09_pass, "ORD-3050 active return RET-9050 requested"))

        # SC-10: ORD-2040, C002, 2 bottles ₹900, ₹400 already refunded
        ord_2040 = session.get(Order, "ORD-2040")
        rfd_2040 = session.get(Refund, "RFD-2040")
        sc10_pass = (
            ord_2040 is not None
            and ord_2040.customer_id == "C002"
            and ord_2040.total_paise == 90000
            and rfd_2040 is not None
            and rfd_2040.amount_paise == 40000
            and rfd_2040.status == "completed"
        )
        results.append(("SC-10", sc10_pass, "ORD-2040 ₹400 already refunded, ₹500 remaining"))

        # SC-11: ORD-1061, C001, delivered T0-1d, bottle ₹450
        ord_1061 = session.get(Order, "ORD-1061")
        sc11_pass = (
            ord_1061 is not None
            and ord_1061.customer_id == "C001"
            and ord_1061.status == "delivered"
            and ord_1061.total_paise == 45000
        )
        results.append(("SC-11", sc11_pass, "ORD-1061 damaged bottle claim scenario"))

        # SC-12: ORD-2037, C002, shipped T0-9d, ETA T0-4d, carrier exception
        ord_2037 = session.get(Order, "ORD-2037")
        shp_2037 = session.get(Shipment, "SHP-2037")
        evt_2037 = session.scalar(
            select(ShipmentEvent).where(ShipmentEvent.shipment_id == "SHP-2037", ShipmentEvent.status == "exception")
        )
        sc12_pass = (
            ord_2037 is not None
            and ord_2037.customer_id == "C002"
            and shp_2037 is not None
            and shp_2037.status == "exception"
            and shp_2037.script_id == "delayed_exception"
            and evt_2037 is not None
            and "address verification pending" in evt_2037.note
        )
        results.append(("SC-12", sc12_pass, "ORD-2037 carrier exception delayed"))

        # SC-13: ORD-5001, C005, delivered T0-2d, ₹499
        ord_5001 = session.get(Order, "ORD-5001")
        sc13_pass = (
            ord_5001 is not None
            and ord_5001.customer_id == "C005"
            and ord_5001.status == "delivered"
            and ord_5001.total_paise == 49900
        )
        results.append(("SC-13", sc13_pass, "ORD-5001 prompt injection test order"))

        # SC-14: ORD-1042 exists for concurrency double-confirmation test
        sc14_pass = ord_1042 is not None and ord_1042.status == "delivered"
        results.append(("SC-14", sc14_pass, "ORD-1042 double-confirmation target available"))

        # SC-15: ORD-3010 (₹499) and ORD-3011 (₹2,450), C003 risk flag (4 historic returns)
        ord_3010 = session.get(Order, "ORD-3010")
        ord_3011 = session.get(Order, "ORD-3011")
        historic_returns_count = session.scalar(
            select(func.count(Return.return_id)).where(
                Return.customer_id == "C003", Return.status == "completed"
            )
        )
        sc15_pass = (
            ord_3010 is not None
            and ord_3010.total_paise == 49900
            and ord_3011 is not None
            and ord_3011.total_paise == 245000
            and historic_returns_count == 4
        )
        results.append(("SC-15", sc15_pass, "ORD-3010/3011 and C003 4 historic completed returns"))

        # SC-16: ORD-4020 COD delivered ₹699, ORD-4021 prepaid placed ₹899
        ord_4020 = session.get(Order, "ORD-4020")
        ord_4021 = session.get(Order, "ORD-4021")
        sc16_pass = (
            ord_4020 is not None
            and ord_4020.payment_method == "cod"
            and ord_4020.payment_status == "cod_collected"
            and ord_4020.status == "delivered"
            and ord_4020.total_paise == 69900
            and ord_4021 is not None
            and ord_4021.payment_method == "prepaid"
            and ord_4021.status == "placed"
            and ord_4021.total_paise == 89900
        )
        results.append(("SC-16", sc16_pass, "ORD-4020 COD delivered & ORD-4021 prepaid placed"))

        # SC-17: ORD-1070, C001, lost shipment ₹998
        ord_1070 = session.get(Order, "ORD-1070")
        shp_1070 = session.get(Shipment, "SHP-1070")
        evt_1070 = session.scalar(
            select(ShipmentEvent).where(ShipmentEvent.shipment_id == "SHP-1070", ShipmentEvent.status == "lost")
        )
        sc17_pass = (
            ord_1070 is not None
            and ord_1070.customer_id == "C001"
            and ord_1070.total_paise == 99800
            and shp_1070 is not None
            and shp_1070.status == "lost"
            and shp_1070.script_id == "lost_in_transit"
            and evt_1070 is not None
        )
        results.append(("SC-17", sc17_pass, "ORD-1070 carrier lost in transit ₹998"))

    # Print results
    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - pass_count

    for sc_id, ok, desc in results:
        status_str = "PASS" if ok else "FAIL"
        print(f"{status_str} {sc_id}: {desc}")

    print(f"\nVerification finished: {pass_count} passed, {fail_count} failed.")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(verify_seed())

