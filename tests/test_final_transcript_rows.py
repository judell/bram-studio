"""The Final transcript page's rows (issue 5: split long turns).

    python3 -m unittest tests/test_final_transcript_rows.py

serve_media.py can't be imported by a test: at import it picks a port, opens
the project and starts serving. So this loads just the rows_of helper (and
the constants it needs) from the file's source with ast, and runs it on
synthetic words.
"""
import ast
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    src = open(os.path.join(REPO, "serve_media.py"), encoding="utf-8").read()
    keep = {"ROW_PARA_MIN", "ROW_SENT_MIN", "SENTENCE_END", "rows_of", "turns_of"}
    nodes = []
    for node in ast.parse(src).body:
        names = [node.name] if isinstance(node, ast.FunctionDef) else \
            [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        if keep & set(names):
            nodes.append(node)
    ns = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "serve_media.py", "exec"), ns)
    return ns


NS = load()


def build():
    # Speaker 1: 1,000 words in paragraphs of 30, 200, 130, 600 (sentences
    # of 10 words) and 40; then speaker 2: a 50-word turn.
    words, para_of, wid = [], {}, 0
    for p, size in enumerate([30, 200, 130, 600, 40]):
        for j in range(size):
            wid += 1
            w = f"w{wid}" + ("." if (j + 1) % 10 == 0 else "")
            words.append((wid, w, 1, "m", 0, float(wid)))
            para_of[wid] = p
    for j in range(50):
        wid += 1
        words.append((wid, f"s{wid}", 2, "m", 0, float(wid)))
        para_of[wid] = 5
    turns = NS["turns_of"](words, {"1": "Ann", "2": "Bob"}, {"1": "red", "2": "blue"})
    return words, para_of, turns, NS["rows_of"](turns, words, para_of)


class Rows(unittest.TestCase):
    def setUp(self):
        self.words, self.para_of, self.turns, self.rows = build()

    def test_index_is_row_order(self):
        self.assertEqual([r["i"] for r in self.rows], list(range(len(self.rows))))

    def test_turn_and_part(self):
        self.assertEqual({r["turn"] for r in self.rows}, {0, 1})
        for t in (0, 1):
            self.assertEqual([r["part"] for r in self.rows if r["turn"] == t],
                             list(range(sum(1 for r in self.rows if r["turn"] == t))))

    def test_words_reproduce_each_turn_in_order(self):
        for t, turn in enumerate(self.turns):
            ids = [x["id"] for r in self.rows if r["turn"] == t for x in r["wl"]]
            self.assertEqual(ids, [w[0] for w in self.words if w[2] == turn["speaker"]])
            text = " ".join(r["text"] for r in self.rows if r["turn"] == t)
            self.assertEqual(text, turn["text"])

    def test_short_turn_stays_one_row(self):
        second = [r for r in self.rows if r["turn"] == 1]
        self.assertEqual(len(second), 1)
        self.assertEqual(len(second[0]["wl"]), 50)

    def test_breaks_at_paragraphs_and_sentences(self):
        first = [r for r in self.rows if r["turn"] == 0]
        sizes = [len(r["wl"]) for r in first]
        # 30+200 (para break after >= 120), then 130 (para break), then the
        # 600-word paragraph in sentence-end rows of 250, then the rest.
        self.assertEqual(sizes, [230, 130, 250, 250, 100 + 40])
        for r in first[:-1]:
            nxt = first[r["part"] + 1]
            last, firstw = r["wl"][-1], nxt["wl"][0]
            self.assertTrue(self.para_of[last["id"]] != self.para_of[firstw["id"]] or last["w"].endswith("."))
        self.assertLessEqual(max(sizes), 250 + 9)

    def test_row_fields(self):
        for r in self.rows:
            self.assertEqual(r["w0"], r["wl"][0]["id"])
            self.assertEqual(r["at"], float(r["w0"]))
            self.assertEqual(r["para"], self.para_of[r["w0"]])
            turn = self.turns[r["turn"]]
            for k in ("speaker", "name", "color", "unsure"):
                self.assertEqual(r[k], turn[k])


if __name__ == "__main__":
    unittest.main()
