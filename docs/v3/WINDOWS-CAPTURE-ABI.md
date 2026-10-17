# Windows yakalama: ABI haritası ve kabul testi

İncelenen kaynak tarihi: **13 Ekim 2026**. GUID'ler ve sıfır tabanlı slotlar Microsoft'un
Windows SDK header'larından karşılaştırıldı. Bağlantılardaki `main` hareketli daldır;
bu belgeyi bir SDK dosyasının SHA-256 sabitlemesi olarak yorumlamayın.
Gerçek WGC/GPU/masaüstü testi Linux'ta yapılmış değildir. Saf ABI ve yaşam döngüsü modelleri,
Windows sürücüsünün başarıyla kare ürettiğini kanıtlamaz.

## Kullanılan tüm IID'ler

| Arayüz | GUID | Kaynak |
|---|---|---|
| `IGraphicsCaptureItemInterop` | `3628e81b-3cac-4c60-b7f4-23ce0e0c3356` | [S02](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/Windows.Graphics.Capture.Interop.h) |
| `IGraphicsCaptureItem` | `79c3f95b-31f7-4ec2-a464-632ef5d30760` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IGraphicsCaptureSessionStatics` | `2224a540-5974-49aa-b232-0882536f4cb5` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IDirect3D11CaptureFramePoolStatics2` | `589b103f-6bbc-5df5-a991-02e28b3b66d5` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IDirect3D11CaptureFramePool` | `24eb6d22-1975-422e-82e7-780dbd8ddf24` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IGraphicsCaptureSession` | `814e42a9-f70f-4ad7-939b-fddcc6eb880d` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IDirect3D11CaptureFrame` | `fa50c623-38da-4b32-acf3-fa9734ad800e` | [S01](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h) |
| `IDirect3DDxgiInterfaceAccess` | `a9b3d012-3df2-4ee3-b8d1-8695f457d3c1` | [S03](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/windows.graphics.directx.direct3d11.interop.h) |
| `IDirect3DDevice` | `a37624ab-8d5f-4650-9d3e-9eae3d9bc670` | [S04](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.directx.direct3d11.h) |
| `IDXGIDevice` | `54ec77fa-1377-44e6-8c32-88fd5f44c84c` | [S06](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/shared/dxgi.h) |
| `ID3D11Device` | `db6f6ddb-ac77-4e88-8253-819df9bbf140` | [S05](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/d3d11.h) |
| `ID3D11DeviceContext` | `c0bfa96c-e089-44fb-8eaf-26f8796190da` | [S05](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/d3d11.h) |
| `ID3D11Texture2D` | `6f15aaf2-d208-4e89-9ab4-489535d34f9c` | [S05](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/d3d11.h) |
| `IClosable` | `30d5a829-7fa4-4026-83bb-d75bae4ea99e` | [S07](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.foundation.h) |

`ID3D11Device` ve `ID3D11DeviceContext` adresleri `D3D11CreateDevice` çıkışlarından alınır;
IID'leri haritada denetim için bulunur, bu iki IID ile ek QI yapılmaz. Frame/session/pool
adresleri de kendi tipli WinRT metodunun çıkışıdır. IUnknown/IInspectable için ayrı GUID
uydurulmaz; bunların miras alınmış slotları kullanılır.

## Çağrılan vtable slotları

Bütün sayılar **0 tabanlı**, miras alınan slotlar dahil. IUnknown: QueryInterface=0,
AddRef=1, Release=2. IInspectable ayrıca GetIids=3, GetRuntimeClassName=4,
GetTrustLevel=5 içerir; bu yüzden ilk WinRT metodu genellikle 6'dır.

| Arayüz | Slot | Metot | Dönüş / kritik argüman | Kaynak |
|---|---:|---|---|---|
| IUnknown / bütün arayüzler | 0 | QueryInterface | HRESULT; GUID*, void** | SDK arayüzleri |
| IUnknown / bütün arayüzler | 2 | Release | ULONG (32 bit); HRESULT değil | SDK arayüzleri |
| IGraphicsCaptureItemInterop | 3 | CreateForWindow | HRESULT; HWND, REFIID, void** | S02 |
| IGraphicsCaptureSessionStatics | 6 | IsSupported | HRESULT; WinRT boolean* (8 bit) | S01 |
| IGraphicsCaptureItem | 7 | get_Size | HRESULT; SizeInt32* | S01 |
| IDirect3D11CaptureFramePoolStatics2 | 6 | CreateFreeThreaded | HRESULT; device*, pixel format, buffer count, SizeInt32 **değer**, pool** | S01 |
| IDirect3D11CaptureFramePool | 7 | TryGetNextFrame | HRESULT; frame** | S01 |
| IDirect3D11CaptureFramePool | 10 | CreateCaptureSession | HRESULT; item*, session** | S01 |
| IGraphicsCaptureSession | 6 | StartCapture | HRESULT | S01 |
| IDirect3D11CaptureFrame | 6 | get_Surface | HRESULT; surface** | S01 |
| IDirect3D11CaptureFrame | 8 | get_ContentSize | HRESULT; SizeInt32* | S01 |
| IDirect3DDxgiInterfaceAccess | 3 | GetInterface | HRESULT; REFIID, void** | S03 |
| ID3D11Device | 5 | CreateTexture2D | HRESULT; D3D11_TEXTURE2D_DESC*, initial-data*, texture** | S05 |
| ID3D11Texture2D | 10 | GetDesc | **void**; D3D11_TEXTURE2D_DESC* | S05 |
| ID3D11DeviceContext | 47 | CopyResource | **void**; destination*, source* | S05 |
| ID3D11DeviceContext | 14 | Map | HRESULT; resource*, subresource, map type, flags, mapped* | S05 |
| ID3D11DeviceContext | 15 | Unmap | **void**; resource*, subresource | S05 |
| IClosable | 6 | Close | HRESULT | S07 |

Slot tuzakları: `ID3D11Texture2D`, IUnknown + ID3D11DeviceChild + ID3D11Resource
metotlarını miras alır. `ID3D11DeviceContext` de ID3D11DeviceChild'dan türediği için
Map/CopyResource sayıları doğrudan metodun görünen sıra numarası değildir.
FramePool'un Recreate=6, TryGetNextFrame=7, add/remove_FrameArrived=8/9,
CreateCaptureSession=10 sırası korunur. Bu uygulama 8/9'u **çağırmaz**; free-threaded
pool'u kısa aralıklı `TryGetNextFrame` ile okur. Dolayısıyla event delegate GUID'i veya
DispatcherQueue gerekmez; olmayan callback API'si kullanılmaz. [S01, S09]

## Export fonksiyonlar ve veri düzeni

`combase.dll`: RoInitialize(uint32)->HRESULT, RoUninitialize()->void,
WindowsCreateString(wchar*,uint32,HSTRING*)->HRESULT,
WindowsDeleteString(HSTRING)->HRESULT,
RoGetActivationFactory(HSTRING,GUID*,void**)->HRESULT. [S23, S24]

`d3d11.dll`: D3D11CreateDevice ve CreateDirect3D11DeviceFromDXGIDevice.
İkincisi IDXGIDevice* alıp **IInspectable*** verir; IDirect3DDevice ile QI bundan sonra yapılır.
D3D11 device/context, HRESULT/out parametreleriyle alınır. [S03, S22]

| Düzen | Boyut / sözleşme |
|---|---|
| GUID | 16 byte; ilk üç alan little-endian, kalan 8 byte olduğu gibi |
| HRESULT / ULONG | signed int32 / unsigned int32; Linux `c_long` genişliği kopyalanmaz |
| HWND / COM / HSTRING | pointer genişliği |
| SizeInt32 | 8 byte; iki signed int32 |
| D3D11_TEXTURE2D_DESC | 44 byte; DXGI_SAMPLE_DESC dahil |
| D3D11_MAPPED_SUBRESOURCE | x64'te 16, x86'da 12 byte; pointer + iki uint32 |

D3D ayarları: hardware driver=1, BGRA_SUPPORT=0x20, SDK_VERSION=7,
DXGI_FORMAT_B8G8R8A8_UNORM=87, iki frame buffer. Staging texture: Usage=3,
BindFlags=0, CPUAccessFlags=0x20000, MiscFlags=0. Map type READ=1, flags=0.
Texture ve frame ContentSize ayrı denetlenir; gerçek `RowPitch` kullanılır. [S01, S05, S17]

## Yaşam döngüsü / başarısızlık

`_WGCSession` yalnız sonlandırılabilir yardımcı süreçte çalışır. MTA RoInitialize(1)
başarılıysa (S_FALSE dahil) bir RoUninitialize ile dengelenir. Çıkış pointer'ları çağrıdan
**önce** scope'a kaydolur; kısmi oluşturma hatasında da elde edilmiş referanslar bırakılır.
Frame'de staging/texture/access/surface bırakılır, frame Close/Release yapılır. Sonra
session, pool, item/factory ve device zinciri ters sırada temizlenir. Map başarılıysa CPU
kopyası hata verse bile Unmap çalışır. Bir Close hatası diğer Release çağrılarını atlatmaz.

Takılan native çağrı normal `finally` akışına dönmeyebilir. Dış deadline'da child/job
sonlandırılır; bu durumda Python'un tek tek Release çalıştırdığı **iddia edilmez**.
Süreç ölümüyle kaynakların işletim sistemine geri dönmesi ayrı güvenlik sınırıdır. [S16]

WGC dış bütçesi 2,5 saniye; iç frame bekleme bütçesi 1,5 saniye. WGC başarısızsa bağımsız
PrintWindow child'ına 1,2 saniye verilir. Sonlandırma/başlatma giderleri bu rakamlara eklenir;
bunlar uçtan uca gerçek-zamanlı garanti değildir. PrintWindow senkrondur, bu yüzden o da
Tk/dinleyici thread'inde çağrılmaz. [S15]

## Gerçek Windows doğrulaması

Tam repoda, önce güncel dosyaları PR dalına yerleştirip testleri çalıştırın:

```powershell
py -3.12 -m unittest discover -s tests -p "v3_yakala_*.py" -v
$env:BEYIN_TEST_WGC = "1"
py -3.12 -m unittest discover -s tests -p "v3_yakala_wgc_test.py" -v
Remove-Item Env:BEYIN_TEST_WGC
```

WGC testi yalnız kendi çizdiği pencereye erişir. Masaüstü kilitliyse, foreground reddedilirse
veya WGC kare vermezse bu testi geçmiş saymayın. Diğer mevcut gerçek Win32 testlerine yeni
`BEYIN_TEST_DESKTOP` engeli eklenmemiştir. Tam repo dışında installer/build/hygiene
bağımlılığı isteyen testler eksik dosya nedeniyle başarısız olabilir; bunlar başarı değildir.

PowerShell'de her komutu çalıştırdıktan sonra geri sayım sırasında hedef pencereye geçin:

```powershell
py -3.12 tests/manual_windows_capture.py --backend wgc --delay 5 --output .\wgc-game.png
py -3.12 tests/manual_windows_capture.py --backend printwindow --delay 5 --output .\printwindow-game.png
py -3.12 tests/manual_windows_capture.py --backend auto --delay 5 --output .\auto-game.png
py -3.12 tests/manual_windows_capture.py --url --delay 5
```

`--backend wgc` hiçbir fallback yapmaz. Başarılı JSON'da `method: WGC` ve beklenen boyut
bulunmalı; dosyayı açıp doğru pencereyi/güncel kareyi kontrol edin. Yanlış/siyah görüntüyü
sırf PNG oluştu diye başarı saymayın. `--backend auto` iki denemeyi ve seçilen yöntemi yazar.
URL komutu adresi konsola yazdığı için özel/token'lı adreslerin çıktısını paylaşmayın.
Bu araç vault'a kart yazmaz; yalnız açıkça istenen PNG dosyasını yazar.

Ardından kurulu vault'ta yeni script yerleştirildikten sonra:

```powershell
py -3.12 .\beyin.py yakala kur
py -3.12 .\beyin.py yakala durum
```

Chrome normal sayfası; Chrome adres çubuğuna yazılmış fakat Enter verilmemiş başka adres;
Opera Air; Notepad; GPU oyununda pencere/kenarlıksız/exclusive-fullscreen; ikinci monitör;
%100/%150/%200 DPI; kaynak değişimi; yükseltilmiş uygulama; Alt+Tab; hızlı iki Enter ve
10 ardışık hotkey senaryolarını karşılaştırın. Tek popup kalmalı; tekrar hotkey eski notu/
kaynakları koruyup öne getirmeli; Esc veya Kapat kayıt yapmadan kapatmalı.

Tam ekran oyunlar, DRM/korumalı pencere, güvenli UAC masaüstü, UIPI/yetki farkı, ekran
kilidi ve HDR renk doğruluğu evrensel başarı kapsamı değildir. WGC yolu SDR BGRA8 üretir;
HDR ton eşleme eklenmemiştir. Koruma aşılmaz, hata halinde ekran BitBlt'a dönülmez. [S08, S21]

## Opera Air: renderer ve MSAA okuma

15 Ekim 2026 eklemesi. Opera'nın üst pencere UIA ağacı sayfa bağlantılarını göstermese de
`EnumChildWindows` ile bulunan `Chrome_RenderWidgetHostHWND` kendi erişilebilirlik
sağlayıcısını sunabilir. Görünür tek aday, `IsChild`, `GetAncestor(GA_ROOT)` ve pencere
PID'si ile sınırlandırılır; foreground ve renderer kimliği okumadan sonra yeniden doğrulanır.

`AccessibleObjectFromWindow(HWND, OBJID_CLIENT=0xfffffffc, IID_IAccessible, void**)`;
IID `618736e0-3c3d-11cf-810c-00aa00389b71`. `IAccessible`, IDispatch'i miras alır:

| Slot | Metot | Argüman/sonuç |
|---|---|---|
| 10 | get_accName | CHILDID_SELF (`VARIANT VT_I4=0`), BSTR* |
| 11 | get_accValue | CHILDID_SELF, BSTR*; açık HTTP(S) URL |
| 13 | get_accRole | CHILDID_SELF, VARIANT*; VT_I4, ROLE_SYSTEM_DOCUMENT=15 |
| 14 | get_accState | CHILDID_SELF, VARIANT*; INVISIBLE=0x8000 / OFFSCREEN=0x10000 reddedilir |

VT_I4 yalnız alt 32 bitten okunur; BSTR `SysFreeString`, VARIANT `VariantClear`, nesne
`Release` ile bırakılır. Başarılı CoInitializeEx/S_FALSE, CoUninitialize ile dengelenir.
API ve slot kaynağı: [Microsoft Windows SDK oleacc.h](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/oleacc.h).

Opera sayfa+imleç okuması aynı sonlandırılabilir `opera` yardımcı sürecinde, mevcut 1 saniye
bütçesinde yapılır. İmleç fiziksel koordinatı üst pencereye karşı doğrulanır; bağlantı araması
renderer HWND'sinin UIA ağacında yapılır. Belge değeri iki okumada, bulunan bağlantı da iki
okumada doğrulanır. Başka sekmenin/iframe'in URL'sini bulmak için belge ağacı taranmaz.

Birim model testleri: `tests/v3_yakala_opera_test.py`. Canlı kabul: Opera Air'de YouTube
ana sayfasındaki gerçek video bağlantısı, önizleme açıkken aynı kart, Instagram gönderi
bağlantısı, adres çubuğu taslağı, pencere/sekme değişimi ve iki monitör. Hiçbir test History
veritabanından tahmin üretmemelidir; belirsizlikte bağlantı yerine mevcut görüntü yolu kalır.

## Kaynaklar

Aşağıdaki SDK/web kaynakları 13 Ekim 2026'da incelendi; yayımlanma tarihi bilinmeyenleri
uydurulmuş tarihle etiketlemiyoruz. CreateForWindow Learn sayfası son güncellemesi
2024-04-11; delayed-render makalesi 2022-06-09. Haritanın doğru olması canlı test yerine geçmez.

- **S01** — [WGC WinRT ABI (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.capture.h)
- **S02** — [IGraphicsCaptureItemInterop (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/Windows.Graphics.Capture.Interop.h)
- **S03** — [DXGI/WinRT interop (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/windows.graphics.directx.direct3d11.interop.h)
- **S04** — [Direct3D11 WinRT interfaces (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.graphics.directx.direct3d11.h)
- **S05** — [D3D11 interfaces and structures (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/d3d11.h)
- **S06** — [DXGI interfaces (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/shared/dxgi.h)
- **S07** — [IClosable (Windows SDK)](https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/winrt/windows.foundation.h)
- **S08** — [CreateForWindow; Windows 10 1903 / build 18362](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow)
- **S09** — [CreateFreeThreaded](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframepool.createfreethreaded?view=winrt-26100)
- **S10** — [UI Automation threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
- **S11** — [AllowSetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-allowsetforegroundwindow)
- **S12** — [SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
- **S13** — [LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)
- **S14** — [SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- **S15** — [PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow)
- **S16** — [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- **S17** — [D3D11_MAPPED_SUBRESOURCE; RowPitch](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/ns-d3d11-d3d11_mapped_subresource)
- **S18** — [CoInitializeEx](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex)
- **S19** — [GetClipboardSequenceNumber](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclipboardsequencenumber)
- **S20** — [Delayed clipboard rendering](https://devblogs.microsoft.com/oldnewthing/20220609-00/?p=106731)
- **S21** — [Screen capture / HDR](https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture)
- **S22** — [D3D11CreateDevice](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-d3d11createdevice)
- **S23** — [RoInitialize](https://learn.microsoft.com/en-us/windows/win32/api/roapi/nf-roapi-roinitialize)
- **S24** — [RoGetActivationFactory](https://learn.microsoft.com/en-us/windows/win32/api/roapi/nf-roapi-rogetactivationfactory)
- **S25** — [Microsoft windows-rs generated WGC projection](https://raw.githubusercontent.com/microsoft/windows-rs/master/crates/libs/windows/src/Windows/Graphics/Capture/mod.rs)
