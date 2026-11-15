from cfflch.api.admission_status.utils import normalize_text


def test_names_are_compared_without_case_accents_or_outer_spaces() -> None:
    assert normalize_text("  BELTRANA CONCEIÇÃO\n") == "beltrana conceicao"
