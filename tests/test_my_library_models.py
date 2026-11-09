import unittest

from zlib_connector.errors import FormatChangedError
from zlib_connector.my_library import BookTagUpdate, LibraryTags, parse_library_tags, parse_saved_book_ids, validate_tags


class LibraryTagTests(unittest.TestCase):
    def test_add_result_exposes_its_favorite_side_effect_without_inventing_book_state(self):
        result = BookTagUpdate("123", "add", ("Science",))
        self.assertTrue(result.adds_to_favorites)
        self.assertEqual(result.to_dict(), {"book_id": "123", "action": "add", "tags": ["Science"],
                                            "adds_to_favorites": True})

    def test_removing_a_tag_does_not_claim_to_remove_the_book_from_favorites(self):
        result = BookTagUpdate("123", "remove", ("Science",))
        self.assertFalse(result.adds_to_favorites)
        self.assertEqual(result.to_dict(), {"book_id": "123", "action": "remove", "tags": ["Science"],
                                            "adds_to_favorites": False})

    def test_catalog_preserves_distinct_labels_and_does_not_expose_opaque_values(self):
        payload = {"success": 1, "tags": {"Science": 4, "science": {"unknown": "private"},
                                           " science ": ["opaque"], "été": None}}
        result = parse_library_tags(payload)
        self.assertEqual(result.names, ("Science", "science", " science ", "été"))
        self.assertEqual(result.to_dict(), {"tags": ["Science", "science", " science ", "été"]})
        payload["tags"]["another"] = 1
        self.assertEqual(len(result.names), 4)

    def test_empty_php_array_and_empty_object_are_both_empty_catalogs(self):
        for value in ([], {}):
            with self.subTest(value=value):
                self.assertEqual(parse_library_tags({"tags": value}), LibraryTags(()))

    def test_missing_or_changed_catalogs_are_not_reported_as_empty(self):
        for payload in (None, [], {}, {"tags": None}, {"tags": "science"}, {"tags": ["science"]},
                        {"tags": {1: 2}}, {"tags": {"": 1}}, {"tags": {" \t": 1}},
                        {"tags": {"unsafe\x1b[31m": 1}}, {"tags": {"unsafe\u202e": 1}}):
            with self.subTest(payload=payload), self.assertRaises(FormatChangedError):
                parse_library_tags(payload)

    def test_catalog_reads_legacy_names_without_applying_mutation_length_limit(self):
        self.assertEqual(parse_library_tags({"tags": {"x" * 31: 2}}).names, ("x" * 31,))

    def test_validation_removes_only_exact_duplicates_and_does_not_rewrite_identity(self):
        self.assertEqual(validate_tags(["Science", "science", "Science", " science ", "a,b", "é", "e\u0301"]),
                         ("Science", "science", " science ", "a,b", "é", "e\u0301"))

    def test_validation_matches_utf16_length_and_accepts_international_labels(self):
        self.assertEqual(validate_tags(("x" * 30, "\U0001f4da" * 15, "日本語")),
                         ("x" * 30, "\U0001f4da" * 15, "日本語"))
        for tags in (["x" * 31], ["\U0001f4da" * 15 + "x"], ["\ud800"]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                validate_tags(tags)

    def test_validation_rejects_empty_values_wrong_types_and_terminal_controls(self):
        for tags in (None, "science", {"science"}, (), [], [None], [1], [True], [[]], [""],
                     ["   "], ["line\nbreak"], ["unsafe\x1b"], ["unsafe\x7f"], ["unsafe\u202e"]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                validate_tags(tags)


class SavedBookIdTests(unittest.TestCase):
    def test_saved_identifiers_keep_order_and_do_not_fabricate_hashes_or_books(self):
        self.assertEqual(parse_saved_book_ids({"success": 1, "list": [123, "456", 123]}),
                         ("123", "456", "123"))
        self.assertEqual(parse_saved_book_ids({"list": []}), ())

    def test_changed_saved_identifier_payload_is_not_an_empty_library(self):
        for payload in (None, [], {}, {"list": {}}, {"list": None}, {"list": "123"}):
            with self.subTest(payload=payload), self.assertRaises(FormatChangedError):
                parse_saved_book_ids(payload)

    def test_invalid_identifiers_fail_closed(self):
        for identifier in (None, True, False, 1.5, [], {}, -1, "", " 123", "1/2", "１２３", "1" * 21):
            with self.subTest(identifier=identifier), self.assertRaises(FormatChangedError):
                parse_saved_book_ids({"list": [identifier]})


if __name__ == "__main__":
    unittest.main()
