import pytest
from kurai.artists import artist_key
from kurai.recommend.vectors import listen_weight

@pytest.mark.parametrize("line,key", [
    ("Artist feat. Guest", "artist"), ("Artist (feat. Guest)", "artist"),
    ("Artist [feat. Guest]", "artist"), ("Artist (ft. Guest)", "artist"),
    ("Artist feat.Guest", "artist"), ("Artist ft Guest", "artist"),
    ("Artist (Featuring Guest)", "artist"), ("Charli XCX • Charlotte Aitchison", "charli xcx"),
    ("Defeat. Something", "defeat. something"), ("The Features", "the features"),
    ("Dance with the Dead", "dance with the dead"), ("Artist", "artist"),
])
def test_guests(line, key):
    assert artist_key(line) == key

@pytest.mark.parametrize("plays,starred", [(0, False), (7, False), (50, False), (50, True), (200, True)])
def test_dislike_outranks_plays(plays, starred):
    assert listen_weight(plays, starred, 1, None) < 0

def test_other_ratings_unchanged():
    assert listen_weight(0, False, 3, None) == 0
    assert listen_weight(0, False, 5, None) == 2.0