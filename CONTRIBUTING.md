# SesliNot'a Katkıda Bulunma

SesliNot'a katkı sunmak istediğiniz için teşekkürler!

## Geliştirme Ortamı

```bash
git clone https://github.com/fornhere/seslinot.git
cd seslinot
uv sync --extra dev
uv run pytest -q
```

Windows ve macOS masaüstü özellikleri (`sounddevice`, `pystray`, `pynput`) için:

```bash
uv sync --extra dev --extra desktop
```

## Temel İlkeler

1. **Çapraz Platform Güvenliği:** `src/seslinot/platform/os_compat.py` katmanını kullanın; üst seviyede POSIX-özel (`fcntl`, `os.getuid`, `os.killpg`) içe aktarımlar yapmayın.
2. **Sıfır Hayalet Atıf & Sır Koruması:** Dış web sorguları `HafizaBridge.sanitize_web_query` gizlilik duvarından, kasaya yazılan her dosya atomik kilit (`exclusive_file_lock`) ve sır maskelemesinden geçmelidir.
3. **İzole Testler:** Tüm testler yalnızca `tmp_path` üzerinde çalışır; gerçek Obsidian kasalarına veya kullanıcı yapılandırmalarına dokunmaz.
4. **Atomik Commit'ler:** Her çalışan özellik veya hata düzeltmesi kendi testleriyle birlikte temiz bir commit olarak gönderilir.

## Pull Request Kontrol Listesi

- [ ] `uv run pytest -q` tüm testleri geçiyor (Linux, macOS ve Windows CI yeşil).
- [ ] Kişisel yollar, API anahtarları veya yerel kullanıcı adları koda girmedi.
- [ ] Omarchy eklentisi değiştiyse `omarchy plugin validate integrations/omarchy-plugin/forn.seslinot` geçti.
