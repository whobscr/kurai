import pytest

from kurai.artists import artist_key


@pytest.mark.parametrize("line,key", [
    ("Artist feat. Guest", "artist"),
    ("Artist (feat. Guest)", "artist"),
    ("Artist [feat. Guest]", "artist"),
    ("Artist (ft. Guest)", "artist"),
    ("Artist feat.Guest", "artist"),
    ("Artist ft Guest", "artist"),
    ("Artist (Featuring Guest)", "artist"),
    ("Charli XCX • Charlotte Aitchison", "charli xcx"),
    # must NOT be stripped
    ("Defeat. Something", "defeat. something"),
    ("The Features", "the features"),
    ("Dance with the Dead", "dance with the dead"),
    ("Artist", "artist"),
])
def test_guest_stripping(line, key):
    assert artist_key(line) == key


def test_idempotent():
    once = artist_key("Artist (feat. Guest)")
    assert artist_key(once) == once