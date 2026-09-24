"""S31b — build_catalog.blank_non_titles: uninformative RTHK episode titles
(the programme name, a fixed label) publish as "" so the app falls back to the
programme title; real titles survive untouched. Stdlib only, no network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_catalog  # noqa: E402
from build_catalog import _tkey, blank_non_titles  # noqa: E402

PROG = {"title_zh": "講東講西", "title_en": "Free as the Wind"}


def eps(*titles):
    return [{"id": str(1000 + i), "title": t, "date": "2026-09-01"} for i, t in enumerate(titles)]


def titles(episodes):
    return [e["title"] for e in episodes]


class TitleKey(unittest.TestCase):
    def test_folds_nfkc_case_punctuation_space_underscore(self):
        self.assertEqual(_tkey("《講東講西》"), "講東講西")
        self.assertEqual(_tkey(" 講東講西。 "), "講東講西")
        self.assertEqual(_tkey("ＦＲＥＥ ＡＳ ＴＨＥ ＷＩＮＤ"), "freeasthewind")   # full-width → NFKC
        self.assertEqual(_tkey("Free_as-the  Wind!"), "freeasthewind")
        self.assertEqual(_tkey(""), "")
        self.assertEqual(_tkey(None), "")
        self.assertEqual(_tkey("・—！？"), "")


class ProgrammeNameMatch(unittest.TestCase):
    def test_programme_name_and_its_variants_are_blanked(self):
        e = eps("講東講西", "《講東講西》", " 講東講西 ", "講東講西。",
                "FREE AS THE WIND", "free_as-the wind!", "ＦＲＥＥ　ａｓ　ｔｈｅ　Ｗｉｎｄ")
        n = blank_non_titles(PROG, e)
        self.assertEqual(titles(e), [""] * 7)
        self.assertEqual(n, 7)

    def test_title_contained_in_the_programme_name_is_blanked(self):
        e = eps("東講西", "廚出鳳城")
        blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["", "廚出鳳城"])

    def test_real_title_kept_verbatim(self):
        e = eps("廚出鳳城", "講東講西：廚出鳳城", "  AI 與香港  ")
        n = blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["廚出鳳城", "講東講西：廚出鳳城", "  AI 與香港  "])  # never rewritten
        self.assertEqual(n, 0)

    def test_empty_and_punctuation_only_titles_end_empty(self):
        e = eps("", "   ", "——", "廚出鳳城")
        n = blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["", "", "", "廚出鳳城"])
        self.assertEqual(n, 2)          # "" was already empty: not counted

    def test_missing_title_key_becomes_empty_string(self):
        e = [{"id": "1", "date": "2026-09-01"}]
        blank_non_titles(PROG, e)
        self.assertEqual(e[0]["title"], "")

    def test_english_programme_title_also_matches(self):
        e = eps("Free as the Wind")
        blank_non_titles({"title_zh": "自由風", "title_en": "Free as the Wind"}, e)
        self.assertEqual(titles(e), [""])

    def test_no_programme_titles_blanks_nothing_by_name(self):
        e = eps("廚出鳳城")
        blank_non_titles({"title_zh": "", "title_en": ""}, e)
        self.assertEqual(titles(e), ["廚出鳳城"])


class LabelRule(unittest.TestCase):
    def test_label_on_80_percent_of_5_is_blanked_real_title_kept(self):
        e = eps("節目內容", "節目內容", "節目內容 ", "《節目內容》", "廚出鳳城")   # 4/5 = 80 %
        n = blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["", "", "", "", "廚出鳳城"])
        self.assertEqual(n, 4)

    def test_label_below_80_percent_is_kept(self):
        e = eps("歌曲選播", "歌曲選播", "歌曲選播", "甲", "乙")                   # 3/5 = 60 %
        blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["歌曲選播", "歌曲選播", "歌曲選播", "甲", "乙"])

    def test_exactly_four_episodes_all_label_is_blanked(self):
        e = eps("Music Box", "music box", "MUSIC-BOX!", "Ｍｕｓｉｃ Ｂｏｘ")        # 4/4, n = 4
        blank_non_titles(PROG, e)
        self.assertEqual(titles(e), [""] * 4)

    def test_three_of_four_is_under_the_share(self):
        e = eps("歌曲選播", "歌曲選播", "歌曲選播", "廚出鳳城")                    # 75 %
        blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["歌曲選播", "歌曲選播", "歌曲選播", "廚出鳳城"])

    def test_fewer_than_four_episodes_untouched_by_label_rule(self):
        e = eps("節目內容", "節目內容", "節目內容")                                # n = 3
        n = blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["節目內容"] * 3)
        self.assertEqual(n, 0)

    def test_share_counts_all_episodes_including_empty_titles(self):
        # 4 labels among 6 episodes (2 already empty) = 67 % < 80 % → kept.
        e = eps("節目內容", "節目內容", "節目內容", "節目內容", "", "")
        blank_non_titles(PROG, e)
        self.assertEqual(titles(e), ["節目內容"] * 4 + ["", ""])

    def test_constants_match_the_ruling(self):
        self.assertEqual(build_catalog.LABEL_SHARE, 0.80)
        self.assertEqual(build_catalog.LABEL_MIN_EPISODES, 4)


if __name__ == "__main__":
    unittest.main()
