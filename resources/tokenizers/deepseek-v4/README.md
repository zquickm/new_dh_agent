# DeepSeek V4 tokenizer

Source: https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip

Downloaded on 2026-09-14. Official archive SHA-256:
`e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`.

The application loads only `deepseek_v4_tokenizer/tokenizer.json` through the
lightweight `tokenizers` runtime. It does not load model weights and does not
execute `deepseek_tokenizer.py`; that script is retained unchanged as upstream
reference material.
