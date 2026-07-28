import unittest

from caspian.tools.builtins.list_uploaded_files_tool import list_uploaded_files


class BuiltinToolTests(unittest.TestCase):
    def test_list_uploaded_files_docstring_matches_signature(self):
        self.assertEqual(
            set(list_uploaded_files.tool_call_schema.model_fields),
            {"include_outline"},
        )


if __name__ == "__main__":
    unittest.main()
