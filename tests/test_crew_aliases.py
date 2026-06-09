from pathlib import Path

from payer_intel.crew import build_name_clause, load_seed


def test_alias_name_clause_single():
    assert build_name_clause("UnitedHealthcare", "") == '"UnitedHealthcare"'
    assert build_name_clause("UnitedHealthcare", None) == '"UnitedHealthcare"'


def test_alias_name_clause_multi():
    clause = build_name_clause(
        "UnitedHealthcare", "UnitedHealth Group|UHG|Optum"
    )
    assert clause == '("UnitedHealthcare" OR "UnitedHealth Group" OR "UHG" OR "Optum")'


def test_alias_name_clause_dedup_case_insensitive():
    clause = build_name_clause("UnitedHealthcare", "unitedhealthcare|UHG")
    assert clause == '("UnitedHealthcare" OR "UHG")'


def test_seed_csv_has_aliases_column():
    rows = load_seed(Path("data/seed_payers.csv"))
    for r in rows:
        assert "search_aliases" in r
    uhc = next(r for r in rows if r["payer_name"] == "UnitedHealthcare")
    assert "UnitedHealth Group" in uhc["search_aliases"]
