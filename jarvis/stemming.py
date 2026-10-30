"""The one stemmer, shared by the wiki (knowledge.py) and vault recall (vault.py)."""


def stem(word):
    """Light suffix stripping, enough that runs/running/run and routine/routines meet."""
    if len(word) <= 3 or not word.isalpha():
        return word
    for suffix in ("ies", "ing", "edly", "ed", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)] + ("y" if suffix == "ies" else "")
            if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "lsz":
                word = word[:-1]
            break
    else:
        if word.endswith("es") and word[:-2].endswith(("sh", "ch", "x", "ss", "z")):
            word = word[:-2]
        elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
    return word[:-1] if word.endswith("e") and len(word) > 4 else word
