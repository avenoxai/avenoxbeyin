"""Text without a backtick is redacted exactly as in 3.17.0: synthetic values only.

The backtick rule for unquoted values (#907, #910) changed only how a backtick
run is read. Compact JSON, connection strings, key=value lists, .env and YAML
lines, shell commands, URLs with credentials, headers and Turkish prose hold no
backtick, so their redacted text and count stay byte for byte as 3.17.0 wrote
them. The expected values below were produced once by the 3.17.0 source; the
test does not need that source to run.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_secrets import redact

# Generated from the 3.17.0 source of beyin_v3_secrets.redact; do not edit by hand.
BASELINE = (
    ('{"password":"abcdefghij","host":"db.example.com","port":5432}',
     '{[REDACTED]:5432}', 1),
    ('{"api_key":"Xy7kL9amN2pQ4zR8","region":"eu-west-1"}',
     '{[REDACTED]}', 1),
    ('{"user":"deploy","token":"tk_demo_1234abcd5678","scope":"read"}',
     '{"user":"deploy",[REDACTED]}', 1),
    ('  "client_secret": "Zq9Wm2Lp7Rt4Kx8V",',
     '  [REDACTED],', 1),
    ('    "DB_PASSWORD": "Hunter2Example!x", "DB_HOST": "localhost"',
     '    [REDACTED], "DB_HOST": "localhost"', 1),
    ('{"access_token": "aT0kEn.Demo.Value99", "expires_in": 3600}',
     '{[REDACTED], "expires_in": 3600}', 1),
    ('{"secret":"short"}',
     '{"secret":"short"}', 0),
    ('[{"name":"api","password":"Pa55w0rdDemo#1"},{"name":"web","password":"Other99Demo"}]',
     '[{"name":"api",[REDACTED]}]', 1),
    ('Server=x;Password=abcdefgh;Database=foo;',
     'Server=x;[REDACTED];Database=foo;', 1),
    ('Server=db.local;User Id=sa;Password=Xy7kL9amN2;Encrypt=true;',
     'Server=db.local;User Id=sa;[REDACTED];Encrypt=true;', 1),
    ('Host=localhost;Port=5432;Username=app;Password=Demo1234Pass;Database=app',
     'Host=localhost;Port=5432;Username=app;[REDACTED];Database=app', 1),
    ('Data Source=srv;Initial Catalog=demo;password=Pq8Rs7Tu6Vw5;Trusted_Connection=False',
     'Data Source=srv;Initial Catalog=demo;[REDACTED];Trusted_Connection=False', 1),
    ('AccountName=demo;AccountKey=QUJDREVGR0hJSktMTU5PUA==;EndpointSuffix=core.windows.net',
     'AccountName=demo;AccountKey=QUJDREVGR0hJSktMTU5PUA==;EndpointSuffix=core.windows.net', 0),
    ('token=abcdefgh,mode=fast',
     '[REDACTED],mode=fast', 1),
    ('api_key=Xy7kL9amN2pQ4zR8,timeout=30,retries=3',
     '[REDACTED],timeout=30,retries=3', 1),
    ('user=bob, password=Sw0rdfish99, role=admin',
     'user=bob, [REDACTED], role=admin', 1),
    ('secret=abcd1234efgh;next=1',
     '[REDACTED];next=1', 1),
    ('mode=fast,token=Lm3Np4Qr5St6,debug=false',
     'mode=fast,[REDACTED],debug=false', 1),
    ('OPENAI_API_KEY=Demo1234NotReal5678',
     '[REDACTED]', 1),
    ('DB_PASSWORD=Xy7#kL9a,mN2$pQ4!',
     '[REDACTED],mN2$pQ4!', 1),
    ('export AWS_SECRET_ACCESS_KEY="DemoSecretAccessKeyValue0123456789abcd"',
     'export [REDACTED]', 1),
    ('STRIPE_SECRET=Abc123Def456Ghi789 # test only',
     '[REDACTED] # test only', 1),
    ('MY_SECRET=short',
     'MY_SECRET=short', 0),
    ('PUBLIC_API_URL=https://example.com/docs',
     'PUBLIC_API_URL=https://example.com/docs', 0),
    ("GITHUB_TOKEN='Ghp1Demo2Value3Here4'",
     '[REDACTED]', 1),
    ('SECRET_KEY = Kx8Vq2Lm7Np4Rt9W',
     '[REDACTED]', 1),
    ('DB_PASSWORD="Xy7#kL9a"mN2$pQ4!',
     '[REDACTED]mN2$pQ4!', 1),
    ('API_KEY: Xy7#kL9a;mN2$pQ4!.',
     '[REDACTED];mN2$pQ4!.', 1),
    ('password: Hunter2Example',
     '[REDACTED]', 1),
    ('  api_key: "Xy7kL9amN2pQ4zR8"',
     '  [REDACTED]', 1),
    ('    secret: Zq9Wm2Lp7Rt4Kx8V # rotate monthly',
     '    [REDACTED] # rotate monthly', 1),
    ('token: abc',
     'token: abc', 0),
    ('db:\n  user: app\n  password: Demo1234Pass\n  host: db.local',
     'db:\n  user: app\n  [REDACTED]\n  host: db.local', 1),
    ('auth_token: >-\n  Lm3Np4Qr5St6Uv7W',
     'auth_token: >-\n  Lm3Np4Qr5St6Uv7W', 0),
    ('export TYPESAFE_API_KEY=Xy7kL9amN2pQ4zR8',
     'export [REDACTED]', 1),
    ('export DB_PASSWORD=Xy7#kL9a mN2 ve devam',
     'export [REDACTED] mN2 ve devam', 1),
    ('setx API_KEY Xy7kL9am;N2pQ4zR8, sonra',
     '[REDACTED];N2pQ4zR8, sonra', 1),
    ("setx API_KEY 'Xy7kL9am'N2pQ4zR8",
     '[REDACTED]N2pQ4zR8', 1),
    ('setx /M TYPESAFE_API_KEY Abcd1234Efgh5678',
     '[REDACTED]', 1),
    ('SETX OPENAI_API_KEY "Xy7kL9amN2pQ4zR8" /M',
     '[REDACTED] /M', 1),
    ('setx TYPESAFE_API_KEY komutunu kullan',
     'setx TYPESAFE_API_KEY komutunu kullan', 0),
    ('set -gx MY_SECRET Qw3Er4Ty5Ui6',
     '[REDACTED]', 1),
    ('[Environment]::SetEnvironmentVariable("DB_PASSWORD", "Xy7kL9amN2pQ4zR8", "User")',
     '[Environment]::[REDACTED], "User")', 1),
    ('setx API_KEY Xy7kL9am,N2pQ4zR8',
     '[REDACTED],N2pQ4zR8', 1),
    ('postgres://app:Demo1234Pass@db.example.com:5432/app',
     'postgres://[REDACTED]@db.example.com:5432/app', 1),
    ('https://deploy:Tk9Lm8Np7@git.example.com/repo.git',
     'https://[REDACTED]@git.example.com/repo.git', 1),
    ('redis://:Sw0rdfish99@cache.local:6379/0',
     'redis://[REDACTED]@cache.local:6379/0', 1),
    ('https://example.com/callback?access_token=Xy7kL9amN2pQ4zR8&state=abc',
     'https://example.com/callback?[REDACTED]&state=abc', 1),
    ('https://example.com/?secret_key=abcd1234efgh5678&format=md',
     'https://example.com/?[REDACTED]&format=md', 1),
    ('Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789',
     'Authorization: [REDACTED]', 1),
    ('Authorization: Token Xy7kL9amN2pQ4zR8',
     '[REDACTED]', 1),
    ('Authorization: Basic ZGVtbzpkZW1vcGFzc3dvcmQ=',
     'Authorization: [REDACTED]', 1),
    ("curl -H 'Authorization: Bearer aBcDeFgHiJkLmNoPqRsTuV12' https://api.example.com",
     "curl -H 'Authorization: [REDACTED]' https://api.example.com", 1),
    ('X-Api-Key: Xy7kL9amN2pQ4zR8',
     'X-[REDACTED]', 1),
    ('| DB_PASSWORD | Xy7kL9amN2pQ4zR8 | prod |',
     '| DB_PASSWORD | Xy7kL9amN2pQ4zR8 | prod |', 0),
    ('2026-11-27T10:00:00Z level=info msg="login" password=Demo1234Pass user=app',
     '2026-11-27T10:00:00Z level=info msg="login" [REDACTED] user=app', 1),
    ('api_key=Xy7kL9am;N2pQ4zR8',
     '[REDACTED];N2pQ4zR8', 1),
    ('Şifre değiştirildi, yeni password=Gizli1234Deger oldu; lütfen kaydet.',
     'Şifre değiştirildi, yeni [REDACTED] oldu; lütfen kaydet.', 1),
    ('Müşteri API key: Xy7kL9amN2pQ4zR8 olarak ayarlandı.',
     'Müşteri [REDACTED] olarak ayarlandı.', 1),
    ('Arşiv tokenı yenilendi ama token=kisa kaldı.',
     'Arşiv tokenı yenilendi ama token=kisa kaldı.', 0),
    ('Kurulumda OPENAI_API_KEY değişkenini ayarla ve terminali yeniden aç.',
     'Kurulumda OPENAI_API_KEY değişkenini ayarla ve terminali yeniden aç.', 0),
    ('Toplantıda parola politikasını konuştuk, kimse şifre yazmadı.',
     'Toplantıda parola politikasını konuştuk, kimse şifre yazmadı.', 0),
    ('Secret: Ab12Cd34Ef56, sonra diğer adıma geç.',
     '[REDACTED], sonra diğer adıma geç.', 1),
    ("password=Abcdefgh1234'yi kullanma, yenisini üret.",
     '[REDACTED] kullanma, yenisini üret.', 1),
    ("İstanbul ofisindeki sunucunun token'ı: Zq9Wm2Lp7Rt4Kx8V",
     "İstanbul ofisindeki sunucunun token'ı: Zq9Wm2Lp7Rt4Kx8V", 0),
    ('tokenizer.pad_token = tokenizer.eos_token',
     'tokenizer.pad_token = tokenizer.eos_token', 0),
    ('os.environ["TYPESAFE_API_KEY"] = "Xy7kL9amN2pQ4zR8"',
     'os.environ[[REDACTED]', 1),
    ("client = Client(api_key='Xy7kL9amN2pQ4zR8', timeout=10)",
     'client = Client([REDACTED], timeout=10)', 1),
    ('const password = process.env.DB_PASSWORD || "Fallback123Pw";',
     'const [REDACTED] || "Fallback123Pw";', 1),
    ('password=Xy7kL9am\r\nnext_line=1',
     '[REDACTED]\r\nnext_line=1', 1),
    ('-----BEGIN PRIVATE KEY-----\nQUJDQUJDQUJDQUJDQUJDQUJD\n-----END PRIVATE KEY-----',
     '[REDACTED]', 1),
    ('Nothing secret here, just a note about deploying the quartz site.',
     'Nothing secret here, just a note about deploying the quartz site.', 0),
)


class SecretBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-secret-baseline-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'

    def test_text_without_a_backtick_is_redacted_as_in_3_17_0(self):
        self.assertGreater(len(BASELINE), 60)
        for text, expected, count in BASELINE:
            with self.subTest(text=text):
                self.assertNotIn('`', text)
                self.assertEqual(redact(text, self.state), (expected, count))

    def test_baseline_lines_joined_into_one_note_are_redacted_as_in_3_17_0(self):
        # A note holds many such lines; each one is still redacted on its own.
        text = '\n'.join(row[0] for row in BASELINE)
        expected = '\n'.join(row[1] for row in BASELINE)
        self.assertEqual(redact(text, self.state), (expected, sum(row[2] for row in BASELINE)))


if __name__ == '__main__':
    unittest.main()
