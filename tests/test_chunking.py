"""Tests for BM25 search and chunking utilities."""

from localforge.chunking import BM25, TEXT_EXTENSIONS, tokenize_bm25


class TestTokenize:
    def test_basic(self):
        tokens = tokenize_bm25("Hello World")
        assert "hello" in tokens
        assert "world" in tokens

    def test_strips_short(self):
        tokens = tokenize_bm25("I am a big dog")
        assert "i" not in tokens  # single char dropped
        assert "am" in tokens
        assert "big" in tokens

    def test_handles_code(self):
        tokens = tokenize_bm25("fn main() { let x = 42; }")
        assert "main" in tokens
        assert "42" in tokens

    def test_lowercases(self):
        tokens = tokenize_bm25("CamelCase SHOUTING")
        assert "camelcase" in tokens
        assert "shouting" in tokens

    def test_empty(self):
        assert tokenize_bm25("") == []

    def test_special_chars_only(self):
        assert tokenize_bm25("!@#$%") == []


class TestBM25:
    def setup_method(self):
        self.corpus = [
            tokenize_bm25("The quick brown fox jumps over the lazy dog"),
            tokenize_bm25("A rust function that handles error propagation"),
            tokenize_bm25("Python async await coroutine event loop"),
            tokenize_bm25("Database query optimization with SQL indexes"),
        ]
        self.bm25 = BM25(self.corpus)

    def test_search_returns_results(self):
        results = self.bm25.search(tokenize_bm25("rust error handling"))
        assert len(results) > 0

    def test_best_match_is_first(self):
        results = self.bm25.search(tokenize_bm25("rust error handling"))
        # Document 1 (rust + error) should be the best match
        assert results[0][0] == 1

    def test_top_k_limits(self):
        results = self.bm25.search(tokenize_bm25("the"), top_k=2)
        assert len(results) <= 2

    def test_no_match_returns_empty(self):
        results = self.bm25.search(tokenize_bm25("xyznonexistent"))
        assert results == []

    def test_score_is_positive(self):
        results = self.bm25.search(tokenize_bm25("python async"))
        for idx, score in results:
            assert score > 0

    def test_empty_corpus(self):
        bm25 = BM25([])
        results = bm25.search(tokenize_bm25("anything"))
        assert results == []

    def test_single_document(self):
        corpus = [tokenize_bm25("hello world")]
        bm25 = BM25(corpus)
        results = bm25.search(tokenize_bm25("hello"))
        assert len(results) == 1
        assert results[0][0] == 0


class TestTextExtensions:
    def test_common_extensions_present(self):
        for ext in [".py", ".rs", ".ts", ".js", ".go", ".java", ".toml", ".yaml", ".md"]:
            assert ext in TEXT_EXTENSIONS

    def test_binary_not_present(self):
        for ext in [".png", ".jpg", ".exe", ".zip", ".pdf"]:
            assert ext not in TEXT_EXTENSIONS
