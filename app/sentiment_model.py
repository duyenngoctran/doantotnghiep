from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


MODEL_DIR = Path(__file__).resolve().parent.parent / "phobert_student_feedback_sentiment"
SENTIMENT_LABELS = ("NEG", "POS", "NEU")

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _load_tokenizer():
    return AutoTokenizer.from_pretrained(str(MODEL_DIR), use_fast=False)


tokenizer = _load_tokenizer()
model = AutoModelForSequenceClassification.from_pretrained(
    str(MODEL_DIR),
    local_files_only=True,
).to(DEVICE)
model.eval()

ID2LABEL: Dict[int, str] = {int(key): value for key, value in model.config.id2label.items()}
LABEL_TO_INDEX = {value.lower(): key for key, value in ID2LABEL.items()}
NEG_INDEX = LABEL_TO_INDEX.get("negative", 0)
NEU_INDEX = LABEL_TO_INDEX.get("neutral", 1)
POS_INDEX = LABEL_TO_INDEX.get("positive", 2)


def _normalize_runtime_label(raw_label: str) -> str:
    normalized = raw_label.strip().lower()
    if normalized.startswith("neg"):
        return "NEG"
    if normalized.startswith("pos"):
        return "POS"
    if normalized.startswith("neu"):
        return "NEU"
    return raw_label.upper()


def _predict_probs(text: str) -> Tuple[str, float, float, float]:
    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        padding=True,
        max_length=256,
    )
    inputs = {key: value.to(DEVICE) for key, value in inputs.items()}

    with torch.inference_mode():
        outputs = model(**inputs)
        logits = outputs["logits"]
        probs = torch.softmax(logits, dim=-1).squeeze(0).cpu()

    label_index = int(torch.argmax(probs).item())
    label = _normalize_runtime_label(ID2LABEL.get(label_index, str(label_index)))
    prob_neg = float(probs[NEG_INDEX].item()) if probs.numel() > NEG_INDEX else 0.0
    prob_pos = float(probs[POS_INDEX].item()) if probs.numel() > POS_INDEX else 0.0
    prob_neu = float(probs[NEU_INDEX].item()) if probs.numel() > NEU_INDEX else max(0.0, 1.0 - prob_neg - prob_pos)
    return label, prob_neg, prob_pos, prob_neu


def predict_sentiment(text: str):
    """Trả về (label, prob_neg, prob_pos) cho các luồng hiện tại của hệ thống."""
    label, prob_neg, prob_pos, _ = _predict_probs(text)
    return label, prob_neg, prob_pos


def predict_sentiment_full(text: str):
    """Trả về (label, prob_neg, prob_pos, prob_neu) cho các bài toán cần đủ 3 nhãn."""
    return _predict_probs(text)
