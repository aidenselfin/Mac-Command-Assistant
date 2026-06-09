import io
import wave

from faster_whisper import WhisperModel

_model: WhisperModel | None = None


class SpeechRecognitionError(Exception):
    pass


def _get_model(model_size: str = "small") -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model


def transcribe(wav_bytes: bytes, model_size: str = "small") -> str:
    if not wav_bytes:
        raise SpeechRecognitionError("오디오 데이터가 없습니다.")

    model = _get_model(model_size)

    buf = io.BytesIO(wav_bytes)
    segments, _ = model.transcribe(buf, language="ko", beam_size=5)

    text = " ".join(seg.text.strip() for seg in segments).strip()
    if not text:
        raise SpeechRecognitionError("음성을 인식하지 못했습니다.")
    return text
