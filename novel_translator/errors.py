"""Public errors deliberately contain no API response bodies or credentials."""


class TranslatorError(Exception):
    pass


class InvalidEpub(TranslatorError):
    pass


class InvalidResponse(TranslatorError):
    """Respons model gagal validasi.

    ``same_batch_retry`` menandai kesalahan *mekanis* yang stokastik
    (ID hilang/duplikat, jumlah tidak cocok, skema salah, teks kosong):
    pipeline boleh mencoba ulang batch sekali (sejak v2.2.3 dengan urutan
    unit dibalik) sebelum membelahnya. Kesalahan deterministik
    (terpotong MAX_TOKENS, terlalu pendek, karakter XML ilegal) memakai
    nilai default False dan langsung dibelah.
    """

    def __init__(self, message: str = "", *, same_batch_retry: bool = False):
        super().__init__(message)
        self.same_batch_retry = same_batch_retry


class RepackRequired(InvalidResponse):
    """Input/output preflight exceeded a budget; no generation was sent."""
    pass


class ApiError(TranslatorError):
    pass


class Paused(TranslatorError):
    pass


class KeyExhausted(TranslatorError):
    """Satu API key mencapai batas hariannya; rotasi ke key berikutnya."""
    pass


class Cancelled(Paused):
    pass


class CacheError(TranslatorError):
    pass
