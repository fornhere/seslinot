-- SesliNot — Omarchy Hyprland statik kısayol şablonu
-- Kullanım: Bu dosyayı ~/.config/hypr/bindings.lua içine kopyalayın veya
-- dofile() ile dahil edin.

-- Sesli not kaydını başlat / bitir (toggle)
o.bind("SUPER + N", "SesliNot kayıt aç/kapa", "seslinot record toggle")

-- Bas-konuş (Push-to-Talk): basılı tutarken kaydeder, bırakınca işler
o.bind_hold(
  "SUPER + ALT + Z",
  "SesliNot bas-konuş başlat",
  "seslinot record start",
  "SesliNot bas-konuş bitir",
  "seslinot record stop"
)

-- Sesli not + doğrudan derin araştırma tetikleme
o.bind("SUPER + ALT + N", "SesliNot araştırma kaydı", "seslinot record toggle --research")

-- Aktif ses kaydını iptal et
o.bind("SUPER + SHIFT + ESCAPE", "SesliNot kaydı iptal et", "seslinot record cancel")

-- Omarchy durum çubuğu SesliNot panelini aç/kapa
o.bind("SUPER + SHIFT + R", "SesliNot paneli aç/kapa", "omarchy-shell -q forn.seslinot toggle")
