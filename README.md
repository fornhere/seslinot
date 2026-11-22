# SesliNot

Linux (Omarchy / Hyprland / PipeWire) için yerel GPU destekli sesli not yakalama ve çok-ajanlı derin araştırma sistemi.

- **Anında Sesli Not:** Tek tuşla konuş (`SUPER + N` toggle veya `SUPER + ALT + Z` bas-konuş), CPU Silero VAD + yerel Whisper (`large-v3-turbo`) ile doğrudan Obsidian Hafıza Kasasına (`/home/forn/Hafıza/sesli-notlar/`) atomik olarak kaydedilsin.
- **"Araştır" Çok-Ajanlı Oturum:** Cümlede araştırma niyeti yakalanırsa arka planda izole bir araştırma oturumu başlatılır; önce kasa bağlamıyla yön tayin edilir (Lead Agent), ardından paralel subagent'lar web + kasa context'i toplar, Python deterministik kaynak/alıntı doğrulaması yapar ve karşı-şirket denetimi sonrası kaynaklı rapor `/home/forn/Hafıza/araştırmalar/` altına yazılır.
- **Omarchy Shell (Quickshell) & Hafıza OS Uyumu:** Canlı bar eklentisi, focus-free OSD, aksiyonlu bildirimler ve Hafıza OS tek-yazıcı / sır-tarama sözleşmelerine tam uyum.
