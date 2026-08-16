# Aliran Video — UM Cares RU2025-T323A

**Status:** dokumen ini mencerminkan **suntingan sebenar yang telah dihantar**,
bukan cadangan. Semua masa diambil terus daripada timeline Premiere.

**Fail akhir:** `UMCares_RU2025-T323A_v8.mp4`
**Durasi:** 233.8s (3:54) · 1920×1080 @ 50fps · −16 LUFS · sari kata boleh dihidup/matikan

---

## Turutan penuh (25 klip)

| Masa | Panjang | Aset | Kandungan |
|------|---------|------|-----------|
| 0:00–0:11 | 11.0s | `card_open_logo` | Kad tajuk + logo UM, MOHE, AYG |
| 0:11–0:18 | 7.2s | `C0006` | Pengacara di backdrop program |
| 0:18–0:23 | 4.8s | `C0001` | Pengacara, sudut dekat |
| 0:23–0:29 | 6.2s | `C0011` | Remaja dengan telefon |
| 0:29–0:42 | 12.8s | `kb_gap1_konteks` | Ken Burns — telefon, skrin scam |
| 0:42–0:49 | 7.0s | `kb_risiko2` | Ken Burns — peranti, risiko digital |
| 0:49–0:56 | 6.7s | `C0008` | Wide dewan, suasana program |
| 0:56–1:06 | 10.3s | `kb_gap2_daftar` | Ken Burns — pendaftaran, sarapan |
| 1:06–1:11 | 5.0s | `kb_daftar2` | Ken Burns — peserta di meja |
| 1:11–1:21 | 9.6s | `C0010` | Penceramah dan hadirin |
| 1:21–1:26 | 5.8s | `C0014` | Peserta bersorak — kuiz |
| 1:26–1:33 | 6.2s | `C0002` | Penyampaian goodie bag |
| **1:33–1:42** | 9.2s | `C0020` | **En. Fikri Mohd Khay (ICYM) — taklimat** |
| **1:42–1:51** | 9.0s | `kb_fikri` | **Ken Burns — En. Fikri bersama peserta** |
| 1:51–1:59 | 8.7s | `C0017` | Sesi interaktif bersama fasilitator |
| 1:59–2:06 | 7.0s | `kb_interaksi` | Ken Burns — kuiz Slido, peserta |
| **2:06–2:13** | 7.0s | `kb_hadiah_speaker` | **Cenderahati kepada penceramah** |
| **2:13–2:20** | 7.0s | `kb_hadiah_pelajar` | **Hadiah kepada peserta** |
| 2:20–2:41 | 21.0s | `card_s5_impak` | Kad statistik: 84% · 88% |
| 2:41–2:55 | 13.9s | `C0018` | Testimoni peserta 1 |
| 2:55–3:05 | 9.6s | `C0019` | Testimoni peserta 2 |
| 3:05–3:27 | 22.0s | `card_s7_statistik` | Kad statistik: 4.77 · 92% · 89.3% · 72% |
| 3:27–3:35 | 8.6s | `C0025` | Foto kumpulan besar |
| 3:35–3:41 | 6.2s | `C0029` | Banner program |
| 3:41–3:54 | 12.0s | `card_close_logo` | Penghargaan + logo Yayasan Taqwa, ICYM, UM Press |

`C0026` telah **dikeluarkan** atas permintaan.

---

## Trek audio

| Trek | Kandungan | Aras |
|------|-----------|------|
| A1 | Audio asal klip (ambien) | **−60 dB (senyap)** |
| A2 | Suara latar BM — `ms-MY-OsmanNeural`, pitch asli | −16 LUFS |
| A3 | Testimoni sebenar `C0018`/`C0019` (dibersihkan) | −16 LUFS |
| Muzik | `Steps_Toward_Common_Ground.mp3` | ikut jadual di bawah |

### Kedudukan suara latar

| Babak | Mula | Panjang |
|-------|------|---------|
| s1 pembukaan | 0:12 | 4.7s |
| s2 konteks | 0:23.5 | 17.2s |
| s3 pengenalan | 0:49.5 | 16.5s |
| s4 aktiviti | 1:11.5 | 16.6s |
| s4b ICYM + hadiah | 1:33 | 22.7s |
| s5 impak | 2:21 | 20.0s |
| s7 statistik | 3:05.5 | 22.7s |
| s8 penutup | 3:28 | 19.7s |

### Jadual aras muzik

| Masa | Aras | Sebab |
|------|------|-------|
| 0:00–0:11 | −3 dB | Kad logo, tiada suara |
| 0:11–2:41 | −20 dB | Di bawah suara latar |
| 2:41–3:05 | −25 dB | Testimoni — suara peserta mesti jelas |
| 3:05–3:48 | −20 dB | Di bawah suara latar (termasuk ayat penutup) |
| 3:48–3:54 | −3 dB | Kad penutup, tiada suara |

Muzik bermula pada **t=25s** dalam trek asal kerana 25 saat pertama terlalu
perlahan (−20.9 dB); ia disambung dengan crossfade kerana trek hanya 169.6s.

---

## Nota teknikal penting

1. **Guna `assets/edit_ready/`, bukan `Photos-1-001/`.** 11 daripada 15 klip asal
   adalah VP9 — Premiere mengimport VP9 sebagai audio sahaja tanpa sebarang ralat.
2. **Turutan mesti ikut naratif.** Suara latar menyebut En. Fikri dahulu, jadi
   `C0020` mesti mendahului segmen ICYM. Ini pernah tersilap dan kelihatan janggal.
3. **Semak aras setiap bahagian, bukan LUFS keseluruhan.** Pada satu versi, LUFS
   keseluruhan nampak betul sedangkan kad pembukaan berada pada −39 dB.
4. Sari kata dijana semula dengan `scripts/make_srt.py` setiap kali masa berubah.
