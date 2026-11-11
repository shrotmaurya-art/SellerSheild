import sys
sys.path.insert(0, 'agent/src')
from sellershield_agent.compose.verifier import verify
from sellershield_agent.compose.templates import orders_list
from sellershield_agent.compose.factsheet import FactSheet
from sellershield_agent.clock import SystemClock

sheet = FactSheet(
    allowed_ids=frozenset(["ORD-1048", "ORD-1042", "ORD-1051", "ORD-1060", "ORD-1061", "ORD-1070"]),
    amounts_paise=frozenset([89900, 120000, 79900, 55000, 45000, 99800]),
    dates=frozenset(),
    relative_facts={},
    supported_claims=frozenset(["shipped", "delivered"]),
    promise_facts=frozenset(),
    rule_texts=(),
    orders=(
        {"order_id": "ORD-1048", "status": "shipped"},
        {"order_id": "ORD-1042", "status": "delivered"},
        {"order_id": "ORD-1051", "status": "delivered"},
        {"order_id": "ORD-1060", "status": "delivered"},
        {"order_id": "ORD-1061", "status": "delivered"},
        {"order_id": "ORD-1070", "status": "shipped"},
    )
)

text = orders_list(sheet, "en")
print("Draft text:", text)
res = verify(text, sheet, "en")
print("Verify ok:", res.ok)
print("Violations:", getattr(res, "violations", []))
