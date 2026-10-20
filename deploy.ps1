# PC에서 실행: 코드를 라즈베리파이로 올리고 서비스를 재시작한다.
# 사용법:  powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#          집 밖에서는 -PiHost <DDNS 주소> -Port <외부 SSH 포트>
param(
    [string]$PiHost = "192.168.100.48",
    [int]$Port = 22,
    [string]$User = "jounghyun"
)
$ErrorActionPreference = "Stop"
$target = "$User@$PiHost"
$archive = Join-Path $env:TEMP "mypie-deploy.tar.gz"

Write-Host "1/3 압축하는 중..."
tar -czf $archive -C $PSScriptRoot --exclude=__pycache__ mypie tests deploy config.example.toml README.md
if ($LASTEXITCODE) { throw "압축 실패" }

Write-Host "2/3 파이로 올리는 중..."
scp -q -P $Port $archive "${target}:/tmp/mypie-deploy.tar.gz"
if ($LASTEXITCODE) { throw "업로드 실패" }

Write-Host "3/3 설치하고 재시작하는 중..."
# 코드 폴더만 새로 바꾸고, 설정(config.toml)·키(.env)·기록(data/)은 그대로 둔다
ssh -p $Port $target "cd ~/My_Pie 2>/dev/null || mkdir -p ~/My_Pie; rm -rf ~/My_Pie/mypie ~/My_Pie/tests ~/My_Pie/deploy && tar -xzf /tmp/mypie-deploy.tar.gz -C ~/My_Pie --warning=no-unknown-keyword && rm /tmp/mypie-deploy.tar.gz && bash ~/My_Pie/deploy/install.sh"
if ($LASTEXITCODE) { throw "설치 실패" }

Remove-Item $archive -ErrorAction SilentlyContinue
Write-Host "완료! 모니터링: http://${PiHost}:8000 (집 안에서)"
