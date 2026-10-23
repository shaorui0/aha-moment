# ASR bench (synthetic `say` samples)

CER = char edit distance after stripping punctuation/space/case; silence ref is empty so any output = hallucination. RTF = elapsed/audio duration (incl. VAD+ffmpeg). RSS = whole-process peak for that backend. first-load = model load time (cache warm unless noted).

| backend | file | CER | RTF | peak RSS MB | first-load s | output |
|---|---|---|---|---|---|---|
| mlx_whisper | en.m4a | 0.00 | 0.679 | 1862 | 0.1 | Let's move the deployment to Thursday and check the latency  |
| mlx_whisper | ja.m4a | 0.03 | 0.208 | 1862 | 0.1 | 明日の朝9時に駅前で友達と待ち合わせをして一緒に京都へ行きます。 |
| mlx_whisper | mixed.m4a | 0.06 | 0.226 | 1862 | 0.1 | 我们把Redis的Timeout改成3秒,然后看Gryfana的P99。 |
| mlx_whisper | silence.m4a | 0.00 | 0.007 | 1862 | 0.1 |  |
| mlx_whisper | zh.m4a | 0.00 | 0.143 | 1862 | 0.1 | 今天下午我们开了一个会,讨论了下个季度的产品规划,大家一致同意先把语音记录功能做完。 |
| sensevoice | en.m4a | 0.09 | 0.313 | 718 | 0.0 | ET'S MOVE THE DEPLOYMENT TO THURSDAY ANDHECK TETENCE DASHBOA |
| sensevoice | ja.m4a | 0.58 | 0.038 | 718 | 0.0 | 明日朝九时駅前友待合一所京都行 |
| sensevoice | mixed.m4a | 0.32 | 0.041 | 718 | 0.0 | 我们把 REDIC THE TIME OUT 改成三秒然后看 GRY FRIENL 的 P 九九 |
| sensevoice | silence.m4a | 0.00 | 0.007 | 718 | 0.0 |  |
| sensevoice | zh.m4a | 0.00 | 0.030 | 718 | 0.0 | 今天下午我们开了一个会讨论了下个季度的产品规划大家一致同意先把语音记录功能做完 |
