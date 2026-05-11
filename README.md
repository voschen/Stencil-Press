# Stencil Press

Extract a PDF's design once. Reproduce it forever.

Stencil Press analyzes the layout, typography, and structure of any PDF and generates new PDFs that preserve the original's visual design — with your new content.

## How it works

1. **Extract** — feed it a PDF, it extracts the full design system into structured JSON: layout, typography, spacing, tables, borders, images
2. **Understand** — an agentic layer semantically analyzes the design, understanding what each element is and how it relates to the content
3. **Adapt** — provide new content, the agent adapts it to fit the original design language while preserving layout integrity
4. **Generate** — outputs a new PDF that looks like it came from the same template

## Use cases

- Regenerate branded reports with new data
- Replicate document templates without access to the source files
- Automate document production at scale

## Status

- [x] PDF → structured JSON extraction
- [ ] Agentic semantic understanding layer
- [ ] Content adaptation and generation

## Usage

pip install -r requirements.txt

python main.py

## Stack

Python · PyMuPDF · pdfplumber · ReportLab · LLM
