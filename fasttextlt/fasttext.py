import bz2
import gzip
import lzma
import pathlib
from typing import BinaryIO, Self, cast
import numpy as np

from fasttextlt.format import load, Model, save


# Converted from Gensim CPython code, convergent with
# [GluonNLP's](https://github.com/dmlc/gluon-nlp/blob/f9be6cd2c3780b3c7e11a1aca189bf8129bc0c0d/gluonnlp/vocab/subwords.py#L171-L275).
# See also the [original
# implementation](https://github.com/facebookresearch/fastText/blob/7842495a4d64c7a3bb4339d45d6e64321d002ed8/src/dictionary.cc#L172)


def utf8_ngrams(word: str, min_n: int, max_n: int) -> list[bytes]:
    """Computes FastText's ngrams of utf-8 characters for a word.

    Parameters
    ----------

    - `word`: A unicode string.
    - `min_n`: The minimum ngram length. Must be strictly positive or consequences.
    - `max_n`: The maximum ngram length. Must be strictly positive and bigger than `min_n` or
      consequences will ensue.

    Notes
    -----

    - The ngrams are computed for `f"<{word}>"`, but the first and last 1-gram (if `min_n == 1`) are
      skipped.

    See Also
    --------

    - [Original
      implementation](https://github.com/facebookresearch/fastText/blob/7842495a4d64c7a3bb4339d45d6e64321d002ed8/src/dictionary.cc#L172)
    - [Gensim's
      implementation](https://github.com/piskvorky/gensim/blob/6591e008f065017adce9d25113a036864e3a9dc6/gensim/models/fasttext_inner.pyx#L677)

    """
    encodings = [c.encode("utf-8") for c in f"<{word}>"]
    n_chars = len(encodings)
    return [
        b"".join(encodings[i:j])
        for i in range(n_chars - 1)
        for j in range(i + min_n, min(n_chars, i + max_n) + 1)
        if not (i == 0 and j == 1)
    ]


FNV_1_32_OFFSET_BASIS = np.uint32(2166136261)
FNV_1_32_PRIME = np.uint32(16777619)


# This could of course be a ufunc but that's not what we are doing here.
@np.errstate(over="ignore")  # You say overflow, I say arithmetic in  ℤ/2³²ℤ, we are not the same
def ft_hash(stream: bytes) -> np.uint32:
    """Calculate the FastText hash of `stream`.

    Reproduces the `hash` method from [Facebook's FastText
    implementation](https://github.com/facebookresearch/fastText/blob/master/src/dictionary.cc) via
    its [Gensim
    implementation](https://github.com/piskvorky/gensim/blob/6591e008f065017adce9d25113a036864e3a9dc6/gensim/models/fasttext_inner.pyx#L619).
    Formally, this is the [32 bits FNV-1a hash
    function](en.wikipedia.org/wiki/Fowler–Noll–Vo_hash_function), which used to be used for
    Python's standard `hash` :-)."""
    res = FNV_1_32_OFFSET_BASIS

    # The loop is annoying, but no way out of it in pure Python, even with np trickery
    for b in np.frombuffer(stream, dtype=np.int8).astype(dtype=np.uint32, copy=False):
        res ^= b
        res *= FNV_1_32_PRIME
    return res


def ft_ngram_hashes_slower(
    word: str, min_n: int, max_n: int, num_buckets: int
) -> np.ndarray[tuple[int], np.dtype[np.intp]]:
    """Calculate the FastText ngrams of `word` and hash them.

    Parameters
    ----------
    - `min_n`: Minimum ngram length
    - `max_n`: Maximum ngram length
    - `num_buckets`: The number of buckets
    """
    encoded_ngrams = utf8_ngrams(word, min_n, max_n)
    return (
        # This could be optimized by using the property that the hash of consecutive n-grams aren't
        # independent (`hash(b"abc") == (hash(b"ab") ^ b) * FNV_1_32_PRIME) so doing it this way is
        # a lot of extra work.
        cast(
            np.ndarray[tuple[int], np.dtype[np.intp]],
            np.array([ft_hash(n) for n in encoded_ngrams], dtype=np.intp),
        )
        % num_buckets
    )


# Don't waste our time in function calls and recomputing the same hash several times (the larger
# max_n, the fasterer)
@np.errstate(over="ignore")
def ft_ngram_hashes(
    word: str, min_n: int, max_n: int, num_buckets: int
) -> np.ndarray[tuple[int], np.dtype[np.intp]]:
    """Calculate the FastText ngrams of `word` and hash them. All in a single function.

    Parameters
    ----------
    - `min_n`: Minimum ngram length
    - `max_n`: Maximum ngram length
    - `num_buckets`: The number of buckets
    """
    encodings = [c.encode("utf-8") for c in f"<{word}>"]
    n_chars = len(encodings)
    max_n = min(n_chars, max_n)

    # I promise. Gauß is with me.
    num_ngrams = (max_n - min_n + 1) * n_chars - ((max_n - min_n + 1) * (max_n + min_n - 2)) // 2
    if min_n == 1:
        num_ngrams -= 2

    res = np.empty(num_ngrams, np.intp)
    idx = 0
    # Only `n_chars-1` to skip `>`
    for i in range(n_chars - 1):
        # This is going to change in-place so copy it
        h = FNV_1_32_OFFSET_BASIS.copy()
        for j in range(i, min(n_chars, i + max_n)):
            # `frombuffer` is no-copy yay
            for b in np.frombuffer(encodings[j], dtype=np.int8).astype(np.uint32, copy=False):
                h ^= b
                h *= FNV_1_32_PRIME
            # Skip "<"
            if 0 == i == j:
                continue
            if j - i + 1 >= min_n:
                res[idx] = h % num_buckets
                idx += 1

    return res


# TODO: repr, str
class FastText:
    def __init__(self, model: Model):
        self.model = model
        self._words: list[str] = list(self.model.raw_vocab.keys())
        self._word_ids: dict[str, int] = {w: i for i, w in enumerate(self._words)}

    @property
    def embedding_matrix(self) -> np.ndarray[tuple[int, int], np.dtype[np.floating]]:
        return cast(np.ndarray[tuple[int, int], np.dtype[np.floating]], self.model.vectors_ngrams)

    def get_word_id(self, word: str) -> int:
        return self._word_ids[word]

    # We could lru cache this. Maybe.
    def get_subword_ids(self, word: str) -> np.ndarray[tuple[int], np.dtype[np.intp]]:
        subword_ids = (
            ft_ngram_hashes(
                word, min_n=self.model.minn, max_n=self.model.maxn, num_buckets=self.model.bucket
            )
            + self.model.nwords
        )
        if (word_id := self._word_ids.get(word)) is not None:
            return cast(
                np.ndarray[tuple[int], np.dtype[np.intp]],
                np.concatenate([np.array([word_id], np.intp), subword_ids]),
            )
        else:
            return subword_ids

    def save_model(self, path: str | pathlib.Path):
        with open(path, "wb") as out_stream:
            save(self.model, out_stream)

    # - TODO: support byteIO?
    # - TODO: support mmap? How to make sure it's closed then? With another function? by accepting a
    #   bytesio and letting the user get it from mmap?
    # - TODO: support zstd for 3.14?
    @classmethod
    def load_model(cls, path: str | pathlib.Path, full_model: bool = False) -> Self:
        """Load a FastText model from a file."""
        path = pathlib.Path(path)
        match path.suffix:
            case ".bz2":
                opener = bz2.open
                safe_load = True
            case ".gz":
                opener = gzip.open
                safe_load = True
            case ".xz":
                opener = lzma.open
                safe_load = True
            case _:
                opener = open
                safe_load = False
        with opener(path, "rb") as in_stream:
            model = load(cast(BinaryIO, in_stream), full_model=full_model, safe_load=safe_load)
        return cls(model)
