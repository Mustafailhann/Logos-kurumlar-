@echo off
chcp 65001 >nul
title LOGOS TECH - Vision 5.0 Alpha 1 GUVENLI TEST
cd /d "%~dp0"

set "KAYNAK_PROGRAM=%~dp0OkulGuvenligi.pyz"
set "TEST_KLASORU=%LOCALAPPDATA%\Sekizdesekiz\OkulGuvenligi_5_0_VISION_ALPHA1_TEST"
set "PROGRAM_KLASORU=%TEST_KLASORU%\program"
set "CALISAN_PROGRAM=%PROGRAM_KLASORU%\LogosTech_Vision_5.0.0-alpha.1.pyz"

if not exist "%KAYNAK_PROGRAM%" (
  echo OkulGuvenligi.pyz bulunamadi. ZIP'i normal bir klasore cikartin.
  pause
  exit /b 1
)

where py >nul 2>nul
if not errorlevel 1 (
  set "PYRUN=py -3"
  goto :python_hazir
)
where python >nul 2>nul
if not errorlevel 1 (
  set "PYRUN=python"
  goto :python_hazir
)

echo Python 3.11 veya daha yeni bir surum bulunamadi.
echo Python kurulumunda "Add python.exe to PATH" secenegini isaretleyin.
pause
exit /b 1

:python_hazir
echo.
echo LOGOS TECH VISION 5.0 ALPHA 1 - GUVENLI TEST MODU
echo Gercek veritabani degistirilmeyecek.
echo Ilk calistirmada mevcut verinin ayri bir test kopyasi alinacak.
echo.
%PYRUN% "%~dp0Alpha_Test_Verisi_Hazirla.py"
if errorlevel 1 (
  echo Test verisi hazirlanamadi. Program baslatilmadi.
  pause
  exit /b 1
)

if not exist "%PROGRAM_KLASORU%" mkdir "%PROGRAM_KLASORU%"
copy /Y "%KAYNAK_PROGRAM%" "%CALISAN_PROGRAM%" >nul
if errorlevel 1 (
  echo Program test klasorune kopyalanamadi.
  pause
  exit /b 1
)

%PYRUN% "%CALISAN_PROGRAM%" --data-dir "%TEST_KLASORU%"
if errorlevel 1 (
  echo.
  echo Program beklenmeyen sekilde kapandi.
  echo Bir sonraki acilista 5.0 kurtarma katmani DB butunlugunu kontrol eder.
  echo Hata kayitlari: %TEST_KLASORU%\logs
  pause
)
