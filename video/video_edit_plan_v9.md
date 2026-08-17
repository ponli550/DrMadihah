# Aliran Video — UM Cares RU2025-T323A (v9)

**Status:** dijana daripada `recipes/v9.json` melalui `umcares render`.
Semua masa di bawah diambil terus daripada `.umcares/resolved.json` — tiada
masa yang dikira secara manual.

**Fail akhir:** `UMCares_RU2025-T323A_v9.mp4`
**Durasi:** 237.06s (3:57.1) · 1920×1080 @ 50fps · sari kata boleh dihidup/matikan

---

## Perubahan daripada v8 (permintaan klien)

| # | Permintaan | Tindakan |
|---|------------|----------|
| 1 | Slide 1: "Buang perkataan KPT, hanya nak logo" | Perkataan "KEMENTERIAN PENDIDIKAN TINGGI" berada **di dalam imej logo**, bukan teks kad. Logo dipotong kepada jata sahaja (`logo_mohe_mark.png`) |
| 2 | Tambah logo Yayasan Taqwa, ICYM, UM Press | Jalur 6 logo |
| 3 | 6 logo di slide pertama **dan** terakhir | Kedua-dua kad dibina semula |
| 4 | 0:51 — kredit rakan strategi korporat | Naratif **dirakam semula** (bukan sari kata sahaja) |
| 5 | 1:01 — 150→100 peserta, 12–17→9–17 tahun | Dirakam semula bersama #4 |
| 6 | 2:58 — buang satu `"` | Dibetulkan |
| 7 | 2:42 — tambah klip 3 budak pegang mic | C0018 → **C0017** → C0019; suara sebenar kekal |
| 8 | Klip ramai-ramai di hujung sekali | C0025 **dipindahkan** ke sebelum kad penutup |

### Keputusan yang perlu diketahui

1. **#4 dan #5 dirakam semula, bukan sekadar sari kata.** Rakaman asal
   menyebut "150 orang" dan "12 hingga 17" dengan kuat. Menukar sari kata
   sahaja bermakna suara menyatakan angka yang salah dalam laporan geran.
   Naratif s3 kini 23.5s (dahulu 16.5s).
2. **C0025 dipindahkan, bukan ditambah.** Ia sudah berada pada 3:27 dalam v8.
   Menambah salinan kedua menelan 8.6s; memindahkannya percuma.
3. **C0017 dipendekkan kepada 4.0s pada kemunculan pertama** (1:51) supaya ia
   boleh dimainkan penuh di sebelah testimoni. Pada panjang penuh di kedua-dua
   tempat, suntingan menjadi 4:02 — melebihi spesifikasi 4 minit.
4. **`kb_s3extra` ditambah ke s3.** Naratif baharu lebih panjang daripada
   rakaman asal babak itu; tanpa klip tambahan, 2.5s akan menjadi **hitam**.

---

## Turutan penuh (27 klip)

| Masa | Panjang | Aset | Babak |
|------|---------|------|-------|
| 0:00.0–0:11.0 | 11.0s | `open_v9` | open |
| 0:11.0–0:18.2 | 7.2s | `C0006.mp4` | s1_pembukaan |
| 0:18.2–0:23.0 | 4.8s | `C0001.mp4` | s1_pembukaan |
| 0:23.0–0:29.2 | 6.2s | `C0011.mp4` | s2_konteks |
| 0:29.2–0:42.0 | 12.8s | `kb_gap1_konteks.mp4` | s2_konteks |
| 0:42.0–0:47.5 | 5.5s | `kb_risiko2.mp4` | s2_konteks |
| 0:47.5–0:54.2 | 6.7s | `C0008.mp4` | s3_pengenalan |
| 0:54.2–1:02.7 | 8.5s | `kb_gap2_daftar.mp4` | s3_pengenalan |
| 1:02.7–1:07.2 | 4.5s | `kb_daftar2.mp4` | s3_pengenalan |
| 1:07.2–1:12.0 | 4.8s | `kb_s3extra.mp4` | s3_pengenalan |
| 1:12.0–1:21.6 | 9.6s | `C0010.mp4` | s4_aktiviti |
| 1:21.6–1:27.4 | 5.8s | `C0014.mp4` | s4_aktiviti |
| 1:27.4–1:33.6 | 6.2s | `C0002.mp4` | s4_aktiviti |
| 1:33.6–1:42.8 | 9.2s | `C0020.mp4` | s4b_icym |
| 1:42.8–1:51.8 | 9.0s | `kb_fikri.mp4` | s4b_icym |
| 1:51.8–1:55.8 | 4.0s | `C0017.mp4` | s4b_icym |
| 1:55.8–2:02.8 | 7.0s | `kb_interaksi.mp4` | s4b_icym |
| 2:02.8–2:08.3 | 5.5s | `kb_hadiah_speaker.mp4` | s4b_icym |
| 2:08.3–2:13.3 | 5.0s | `kb_hadiah_pelajar.mp4` | s4b_icym |
| 2:13.3–2:34.3 | 21.0s | `s5_impak` | s5_impak |
| 2:34.3–2:48.2 | 13.9s | `C0018.mp4` | testimoni |
| 2:48.2–2:56.9 | 8.7s | `C0017.mp4` | testimoni |
| 2:56.9–3:06.5 | 9.6s | `C0019.mp4` | testimoni |
| 3:06.5–3:30.3 | 23.7s | `s7_statistik` | s7_statistik |
| 3:30.3–3:36.5 | 6.2s | `C0029.mp4` | s8_penutup |
| 3:36.5–3:45.1 | 8.6s | `C0025.mp4` | s8_penutup |
| 3:45.1–3:57.1 | 12.0s | `close_v9` | s8_penutup |

---

## Trek audio

| Trek | Kandungan | Aras |
|------|-----------|------|
| A1 | Audio asal klip (ambien) | −60 dB (senyap) |
| A2 | Suara latar BM — `ms-MY-OsmanNeural` | −16 LUFS |
| A3 | Testimoni sebenar C0018 / **C0017** / C0019 (dibersihkan) | −16 LUFS |
| Muzik | `Steps_Toward_Common_Ground.mp3` | ikut jadual di bawah |

### Kedudukan suara

| Babak | Mula | Panjang | Trek |
|-------|------|---------|------|
| s1_pembukaan | 0:11.5 | 4.7s | trek 1 |
| s2_konteks | 0:23.5 | 17.2s | trek 1 |
| s3_pengenalan | 0:48.0 | 23.5s | trek 1 |
| s4_aktiviti | 1:12.5 | 16.6s | trek 1 |
| s4b_icym | 1:34.1 | 22.7s | trek 1 |
| s5_impak | 2:13.8 | 20.0s | trek 1 |
| testimoni | 2:34.3 | 0.0s | trek 2 |
| testimoni | 2:48.2 | 0.0s | trek 2 |
| testimoni | 2:56.9 | 0.0s | trek 2 |
| s7_statistik | 3:07.0 | 22.7s | trek 1 |
| s8_penutup | 3:30.8 | 19.7s | trek 1 |

### Jadual aras muzik

| Masa | Aras |
|------|------|
| sehingga 0:11.0 | -3 dB |
| sehingga 2:34.3 | -20 dB |
| sehingga 3:06.5 | -25 dB |
| sehingga 3:50.9 | -20 dB |
| sehingga akhir | -3 dB |

Aras −20 dB berterusan sehingga 230.9s kerana naratif terakhir
tamat pada 230.44s. Mengangkat muzik pada sempadan
babak — bukan pada penghujung ayat — ialah punca ayat penutup v8 sukar didengar.

---

## Nota teknikal

1. **Guna `assets/edit_ready/`.** Klip asal adalah VP9; Premiere mengimport VP9
   sebagai audio sahaja tanpa sebarang ralat.
2. **`resolve` tidak akan membina jika babak lebih pendek daripada naratifnya.**
   Ia melaporkan babak tersebut dan berhenti, kerana bakinya akan menjadi hitam.
3. **Rakaman semula hanya untuk babak yang berubah.** Suara neural tidak
   menghasilkan fail yang sama setiap kali; merakam semula babak yang tidak
   berubah akan menggeser seluruh suntingan.
4. **BELUM SIAP:** C0017 (168.2s–176.9s) belum mempunyai sari kata — kandungan
   pertuturannya perlu ditranskripsikan.
