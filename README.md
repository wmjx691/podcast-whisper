# Podcast Whisper Transcriber 🎙️ -> 📝

**Podcast Whisper Transcriber** is an automated pipeline designed to convert podcast audio into searchable, readable text transcripts.

Built with Python, this project leverages **RSS feed parsing** to automatically fetch episodes and utilizes OpenAI's **Whisper V3 Large model** for high-accuracy speech-to-text conversion, supporting multi-speaker detection and mixed-language (Mandarin/Hokkien) transcription.

## 🚀 Key Features

- **Automated RSS Parsing**: Seamlessly integrates with major podcast hosting platforms (e.g., SoundOn, Firstory) to fetch episode metadata.
- **Smart Downloader**: Automatically identifies and downloads audio files (MP3/M4A) from feeds.
- **AI-Powered Transcription**: Powered by `faster-whisper` (CTranslate2 backend) for optimized inference speed on local machines.
- **High Accuracy**: Supports mixed-language recognition (Taiwanese Hokkien & Mandarin) using the Whisper Large-V3 model.
- **Precision Timestamps**: Generates transcripts with `[MM:SS]` timestamps and word-level alignment support.

## 🛠️ Tech Stack

- **Language**: Python 3.10
- **Core AI Model**: [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI Whisper V3)
- **Data Processing**: Feedparser, Requests, Pandas
- **Audio Processing**: FFmpeg
- **Environment**: WSL2 (Ubuntu) + Miniconda

## 📂 Project Structure

```text
├── src/
│   ├── rss_parser.py    # RSS feed parser and audio downloader
│   ├── transcriber.py   # Whisper model inference engine (In Progress)
├── data/                # Stores downloaded audio and transcripts (Git-ignored)
├── requirements.txt     # Python dependencies
└── README.md            # Project documentation
```
## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- FFmpeg installed (`sudo apt install ffmpeg`)
- GPU recommended (NVIDIA) for faster inference, but CPU is supported.

### Installation

1. Clone the repository:
   ```bash
   git clone [https://github.com/YourUsername/podcast-whisper.git](https://github.com/YourUsername/podcast-whisper.git)
   ```
2. Install dependencies:
   ```bash
   pip install faster-whisper feedparser requests tqdm
   ```
---
Created by Kevin Yu