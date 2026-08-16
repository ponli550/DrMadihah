# Music Brief — Video UM Cares (RU2025-T323A)

Untuk dijana dengan aplikasi AI (Gemini / Lyria / Suno / Udio).
Timing di bawah diambil daripada **suntingan sebenar**, bukan anggaran.

---

## Ringkasan teknikal

| Perkara | Nilai |
|---|---|
| Panjang diperlukan | **3 minit 45 saat** (225s) — jana 4:00 dan potong |
| Tempo | 90–105 BPM (bukan 120–130; naratif ini perlahan dan reflektif) |
| Kunci | Major — C, D atau F major |
| Vokal | **TIADA vokal langsung.** Ada suara latar sepanjang video |
| Format | WAV atau MP3 320kbps, stereo, 48 kHz |
| Aras | Jana pada aras normal; saya akan turunkan ke −24 dB di bawah suara |

---

## Prompt untuk aplikasi AI

> Instrumental corporate documentary background music, 95 BPM, C major, warm and
> hopeful but restrained. Solo piano leads with soft sustained strings underneath
> and light acoustic guitar. Add a gentle kick and soft shaker from the one-minute
> mark for quiet forward motion. No vocals, no lyrics, no vocal chops, no spoken
> word. No heavy drums, no brass stabs, no cinematic risers or impact hits.
> Keep the midrange uncluttered so a male speaking voice sits clearly on top.
> Emotional arc: reflective and slightly serious at the start, gradually warmer
> and more optimistic, gentle uplift at 2:20, calm resolved ending that fades
> naturally. Consistent dynamics — avoid sudden loud swells. Community programme
> for teenagers, educational and sincere in tone, not corporate-slick.

**Jika aplikasi meminta gaya rujukan:** "uplifting corporate acoustic",
"documentary underscore", "hopeful piano and strings".

**Elakkan sebut:** epic, trailer, cinematic drums, EDM, hip hop, drop, anthem.

---

## Struktur mengikut babak sebenar

| Masa | Babak | Mood muzik | Catatan |
|---|---|---|---|
| 0:00–0:10 | Kad logo pembukaan | **Muzik penuh** | Tiada suara latar — muzik boleh menonjol |
| 0:10–0:22 | Pembukaan | Turun perlahan | Suara latar bermula 0:11 |
| 0:22–0:48 | Konteks masalah | Rendah, sedikit serius | Bahagian paling gelap dari segi nada |
| 0:48–1:10 | Pengenalan program | Mula menghangat | |
| 1:10–1:32 | Aktiviti dan modul | Positif, ada pergerakan | |
| 1:32–2:15 | Sesi ICYM + penyampaian hadiah | Ceria, bertenaga sederhana | Bahagian paling meriah |
| 2:15–2:44 | Kad statistik impak | Reflektif | Grafik sahaja — muzik boleh naik sedikit |
| 2:44–3:08 | Testimoni | **Paling lembut** | Suara peserta sebenar — muzik hampir hilang |
| 3:08–3:32 | Kad statistik maklum balas | Naik semula | |
| 3:32–3:52 | Penutup | **Puncak emosi lembut** | Ayat akhir: "Bersama, kita lindungi generasi muda" |
| 3:52–4:02 | Kad logo penutup | Selesai dan reda | Fade out penuh |

---

## Yang paling penting

**Muzik ini akan berada di bawah suara latar hampir sepanjang video.** Sebab itu:

1. **Jangan ada melodi yang terlalu menonjol dalam julat 200 Hz – 4 kHz** — itu julat
   suara manusia. Piano dan gitar yang lembut selamat; solo biola yang tinggi tidak.
2. **Dinamik mesti rata.** Muzik yang tiba-tiba kuat akan melawan suara latar.
3. **Tiada kesan "impact" atau "riser"** — ia akan bertembung dengan potongan babak.
4. **Bahagian testimoni (2:44–3:08) mesti boleh dilembutkan sehingga hampir senyap**
   tanpa terasa janggal. Elakkan melodi berterusan di situ.

---

## Selepas anda jana

Letakkan fail dalam `video/music/` dan beritahu saya. Saya akan:

- Turunkan ke **−24 dB** di bawah naratif, **−18 dB** pada kad logo dan bahagian tanpa suara
- Lembutkan ke **−30 dB** semasa testimoni (2:44–3:08)
- Fade in 1.5s pada permulaan, fade out 3s pada penghujung
- Pastikan campuran akhir kekal pada **−16 LUFS** (atau −14 LUFS jika untuk web)

Jika versi jana pertama terlalu sibuk, minta semula dengan
*"sparser, fewer instruments, more space between notes"*.
