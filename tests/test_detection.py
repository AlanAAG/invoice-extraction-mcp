"""Prove each validation check actually fires. A check that never fails is decoration."""
import asyncio, sys, pathlib, copy
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from src.profile import Registry

reg = Registry()
prof = reg.get("sap_b1_supplier_statement")

class FakeTable:
    rows=[]; orphan_lines=[]; unassigned_words=[]
    words_in_table_region=100; words_claimed=100; overlong_rows=0

def base():
    return [
        {"line_no":1,"amount":-100.0,"running_balance":-100.0,"bp_reference_no":"SI/001","document_no":"1","posting_date":"2025-01-01"},
        {"line_no":2,"amount":-50.0,"running_balance":-150.0,"bp_reference_no":"SI/002","document_no":"2","posting_date":"2025-01-02"},
        {"line_no":3,"amount":-25.0,"running_balance":-175.0,"bp_reference_no":"SI/003","document_no":"3","posting_date":"2025-01-03"},
    ]
SUMMARY = {"buckets":{"Balance Due":-175.0}}

def check(name, records, summary=SUMMARY, table=None, expect_fail=None):
    v = prof.validate(records, summary, table or FakeTable())
    failed = [c["check"] for c in v["checks"] if not c["passed"]]
    hit = expect_fail in failed
    print(f"{'PASS' if hit else 'FAIL'}  {name:34s} -> caught by {failed or 'nothing'}")
    return hit

ok = []
# Control: clean data must pass everything
v = prof.validate(base(), SUMMARY, FakeTable())
ok.append(v["ok"]); print(f"{'PASS' if v['ok'] else 'FAIL'}  clean data validates")

# 1. One amount misread (e.g. digit dropped) -> chain localises it to line 2
r = base(); r[1]["amount"] = -5.0
ok.append(check("misread amount on line 2", r, expect_fail="running_balance_chain"))

# 2. Rows swapped -> sum still correct, chain catches ordering
r = base(); r[1], r[2] = copy.deepcopy(r[2]), copy.deepcopy(r[1])
ok.append(check("rows out of order", r, expect_fail="running_balance_chain"))

# 3. Duplicated row -> sum would be wrong too, but chain names the row
r = base(); r.insert(2, copy.deepcopy(r[1]))
for i,x in enumerate(r,1): x["line_no"]=i
ok.append(check("duplicated row", r, expect_fail="running_balance_chain"))

# 4. Mangled reference (bare wrap fragment) -> arithmetic is perfect, shape check catches
r = base(); r[0]["bp_reference_no"] = "007"
ok.append(check("mangled reference number", r, expect_fail="field_matches:bp_reference_no"))

# 5. Dropped continuation -> unclaimed words
class T(FakeTable): words_claimed=94; unassigned_words=[{"page":0,"text":"00007","x0":124,"top":288}]
ok.append(check("unclaimed words on page", base(), table=T(), expect_fail="word_coverage"))

# 6. Ageing block disagrees with closing balance
ok.append(check("summary disagrees", base(), summary={"buckets":{"Balance Due":-999.0}},
                expect_fail="summary_equals_last"))

# 7. Missing required field
r = base(); r[2]["posting_date"] = None
ok.append(check("missing required field", r, expect_fail="required_fields"))

print("\nAll green." if all(ok) else f"\n{ok.count(False)} check(s) did not fire.")
sys.exit(0 if all(ok) else 1)
