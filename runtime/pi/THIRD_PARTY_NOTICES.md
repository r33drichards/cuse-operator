# Reference and third-party notices

This is a new implementation of the Cuse controller protocol and ownership model.
Its Pi/Cloudflare storage, root conversation and recovery-alarm integration follows
patterns documented in Earendil Works' reference:

- https://earendil.com/posts/pi-durable-on-cloudflare/
- https://github.com/earendil-works/pi-durable-cloudflare/tree/dcb56ce2d174babb3f00112ac5b8cd597e58ab45
- Reference files examined: `src/session.ts`, `src/chat.ts`, `src/index.ts`,
  `test/recovery.test.ts`, `test/storage.test.ts`, package and Worker configuration.

The unauthenticated demo routes, demo model choice, UI and example tools are not
used. Pinned runtime dependencies are `@earendil-works/{chord,pi-ai,pi-durable}`
1.2.0; their license notices ship in the installed packages.

The reference integration patterns are MIT-licensed. Its notice is retained here:

MIT License

Copyright (c) 2026 Earendil Works contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
