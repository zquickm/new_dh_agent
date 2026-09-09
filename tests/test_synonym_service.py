import unittest
from unittest.mock import patch

from services.synonym_service import (
    build_synonym_matcher,
    expand_query_synonyms,
    replace_synonym_matcher,
)


class SynonymServiceTest(unittest.TestCase):
    def setUp(self):
        matcher = build_synonym_matcher([
            {"standardTerm": "溶解氧", "terms": [{"term": "DO"}, {"term": "溶氧"}]},
            {"standardTerm": "pH值", "terms": [{"term": "pH"}]},
            {"standardTerm": "水温(水文)", "terms": [{"term": "SST"}]},
            {"standardTerm": "水文要素", "terms": [{"term": "水文"}]},
        ])
        matcher_patch = patch("services.synonym_service._active_synonym_matcher", matcher)
        matcher_patch.start()
        self.addCleanup(matcher_patch.stop)

    def test_matching_rules(self):
        cases = {
            "查询do和溶氧": "查询do（溶解氧）和溶氧（溶解氧）",
            "溶解氧和pH值": "溶解氧和pH值",
            "DO2 ADO DO": "DO2 ADO DO（溶解氧）",
            "未命中": "未命中",
            "": "",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(expand_query_synonyms(question), expected)

    def test_repeated_expansion_preserves_annotations_and_expands_other_aliases(self):
        question = "DO（溶解氧） DO(溶解氧) SST pH"
        expanded = "DO（溶解氧） DO(溶解氧) SST（水温(水文)） pH（pH值）"
        self.assertEqual(expand_query_synonyms(question), expanded)
        self.assertEqual(expand_query_synonyms(expanded), expanded)
        # Both parenthesis styles must shield the whole standard term.
        self.assertEqual(expand_query_synonyms("SST(水温(水文))"), "SST(水温(水文))")

    def test_first_alias_mapping_wins_and_standard_terms_are_preserved(self):
        replace_synonym_matcher([
            {"standardTerm": "水深", "terms": [{"term": "WD"}]},
            {"standardTerm": "风向", "terms": [{"term": "WD"}, {"term": "水深"}]},
        ])
        self.assertEqual(expand_query_synonyms("WD 水深"), "WD（水深） 水深")

    def test_failed_replacement_keeps_active_matcher(self):
        with self.assertRaises(RuntimeError):
            replace_synonym_matcher([])
        self.assertEqual(expand_query_synonyms("DO"), "DO（溶解氧）")
