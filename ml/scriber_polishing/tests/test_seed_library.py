from scriber_polishing.seed_library import SeedFamily, select_seed


def test_seed_selection_is_reproducible_and_covers_distinct_families() -> None:
    first = select_seed(41)
    second = select_seed(41)

    assert first == second
    assert first.family in set(SeedFamily)
    assert select_seed(1).family != select_seed(2).family
