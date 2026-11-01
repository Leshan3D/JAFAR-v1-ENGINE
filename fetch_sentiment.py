"""
SENTIMENT DATA MODULE
Sources:
  1. Fear & Greed Index - alternative.me, free public endpoint, no key required
  2. FinBERT - open-source model, runs LOCALLY, no API, no limits, free forever
     First run downloads ~400MB from Hugging Face, then works offline.
     Requires: pip install transformers (already installed)
"""

import requests
import pandas as pd


def fetch_fear_greed_index(limit=7):
    """
    Fetch the crypto Fear & Greed Index from alternative.me.
    Free, no API key, no signup required.

    limit : number of days of history (1 = today only, 7 = last week)

    Returns DataFrame with columns: date, value (0-100), classification
      0-24  = Extreme Fear
      25-49 = Fear
      50-54 = Neutral
      55-74 = Greed
      75-100 = Extreme Greed
    """
    url = f"https://api.alternative.me/fng/?limit={limit}&format=json"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()

    df = pd.DataFrame(data["data"])
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float).astype(int), unit="s")
    df = df.rename(columns={
        "timestamp": "date",
        "value_classification": "classification"
    })
    df["value"] = df["value"].astype(int)
    df = df[["date", "value", "classification"]]

    return df


class FinBERTSentiment:
    """
    Open-source FinBERT model for financial sentiment scoring.
    Downloads once from Hugging Face (~400MB), then runs fully offline.
    No ongoing API cost, no rate limits ever.

    Usage:
        finbert = FinBERTSentiment()
        scores = finbert.score(["Bitcoin rises on ETF approval news"])
        print(scores)
    """

    def __init__(self):
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch

        print("Loading FinBERT model...")
        print("(First run downloads ~400MB from Hugging Face - subsequent runs are instant)")
        self.tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        self.model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        self.torch = torch
        self.labels = ["positive", "negative", "neutral"]
        print("FinBERT ready.")

    def score(self, headlines):
        """
        headlines : list of strings (news headlines or article titles)

        Returns DataFrame with columns:
          headline, sentiment (positive/negative/neutral),
          confidence, positive_score, negative_score, neutral_score
        """
        results = []
        for headline in headlines:
            inputs = self.tokenizer(
                headline,
                return_tensors="pt",
                truncation=True,
                max_length=128
            )
            with self.torch.no_grad():
                outputs = self.model(**inputs)

            probs = self.torch.nn.functional.softmax(outputs.logits, dim=-1)[0]
            top_idx = int(self.torch.argmax(probs))

            results.append({
                "headline": headline,
                "sentiment": self.labels[top_idx],
                "confidence": round(float(probs[top_idx]), 4),
                "positive_score": round(float(probs[0]), 4),
                "negative_score": round(float(probs[1]), 4),
                "neutral_score": round(float(probs[2]), 4),
            })

        return pd.DataFrame(results)


if __name__ == "__main__":
    # Test 1: Fear & Greed Index
    print("=" * 50)
    print("TEST 1: Fear & Greed Index")
    print("=" * 50)
    fng = fetch_fear_greed_index(limit=3)
    print(fng.to_string())
    print()

    # Test 2: FinBERT sentiment scoring
    print("=" * 50)
    print("TEST 2: FinBERT Sentiment")
    print("=" * 50)
    finbert = FinBERTSentiment()
    test_headlines = [
        "Bitcoin surges past key resistance as institutional demand grows",
        "Crypto markets crash amid regulatory crackdown fears",
        "Oil prices steady as ceasefire holds in the Middle East",
        "Federal Reserve signals potential rate cut in September",
        "Bitcoin hits multi-year low as selloff deepens"
    ]
    scores = finbert.score(test_headlines)
    print(scores[["headline", "sentiment", "confidence"]].to_string())