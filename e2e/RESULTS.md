# Aha e2e results

Run: 2026-10-09T04:12:19+09:00  
Real ASR (`aha_asr`): available  
Each scenario runs against its own uvicorn (temp data dir, temp token, free port); ~/aha_data and the launchd server are never touched.  
`f` = fake ASR (stub + ffmpeg volumedetect VAD, tests server logic only), `r` = real aha_asr.

| id | scenario | result | secs | detail |
|---|---|---|---|---|
| 1 | happy path 10 segs | **PASS** | 1.9 | 10x201, queue empty, 10 audio on disk, all 10 processed(fake ASR) |
| 2 | duplicate upload after lost ACK | **PASS** | 1.0 | 201 then 200 exists; 1 item/1 audio on disk; same-id-different-content -> 409 (disk untouched) |
| 3 | server down mid-batch (kill -9) | **PASS** | 4.4 | 4 acked pre-kill, 32 neterr while down, all 12 delivered after restart, sha verified, 0 dup, all processed |
| 4 | corrupted upload (sha mismatch) | **PASS** | 0.7 | wrong sha -> 422, file+sidecar kept, backoff set, nothing on server/.tmp; truncated body -> 422, clean retry -> 201 |
| 5f | crash during processing [fake ASR] | **PASS** | 1.5 | kill -9 with in-flight {'processing': 1, 'pending': 3}; after restart 4/4 done, 1 transcript each, quick dispatched once [fake ASR] |
| 5r | crash during processing [real ASR] | **PASS** | 29.4 | kill -9 with in-flight {'processing': 1, 'pending': 3}; after restart 4/4 done, 1 transcript each, quick dispatched once [real ASR] |
| 6 | quick path wait=1 [real ASR] | **PASS** | 5.7 | real ASR: multipart + raw both 200 with text in 3.5s(first); inbox has 2 lines. text='我们把Reduce的Timeout改成3秒,然后看Gryfana的P99延迟有没有下降。' |
| 6f | quick path wait=1 [fake ASR, plumbing only] | **PASS** | 0.9 | fake ASR: multipart + raw both 200 with text in 0.1s(first); inbox has 2 lines. text='fake transcript of q20261009T040554-0a7aaddd' |
| 7f | silent segment [fake VAD] | **PASS** | 1.1 | fake ASR: status=silent, no transcript files, audio kept; quick+silent -> status silent text '' |
| 7r | silent segment [real VAD] | **PASS** | 1.5 | real ASR: status=silent, no transcript files, audio kept; quick+silent -> status silent text '' |
| 8 | throughput 60x60s [real ASR] | **PASS** | 382.0 | upload 60x60s in 0.4s; ASR backlog drained in 381s for 3600s audio (RTF 0.106, 9.5x realtime); counts {'done': 60} |
