#!/usr/bin/env bash
# 不用 Gradle。四个类、零依赖，aapt2 + javac + d8 + apksigner 就够了。
set -euo pipefail

SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
BT="$SDK/build-tools/34.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
[ -x "$BT/d8" ] || { echo "找不到 build-tools/34.0.0"; exit 1; }
[ -f "$PLAT" ] || { echo "找不到 android-34"; exit 1; }

cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes build/dex

echo "1/5 javac"
javac -encoding UTF-8 -source 11 -target 11 -classpath "$PLAT" \
      -d build/classes $(find src -name '*.java')

echo "2/5 d8"
find build/classes -name '*.class' > build/classes.txt
$BT/d8 --lib "$PLAT" --min-api 26 --output build/dex @build/classes.txt

echo "3/5 aapt2 link"
$BT/aapt2 link -o build/unsigned.apk -I "$PLAT" \
     --manifest AndroidManifest.xml \
     --min-sdk-version 26 --target-sdk-version 34

echo "4/5 zip"
cp build/dex/classes.dex build/classes.dex
(cd build && zip -q -u unsigned.apk classes.dex)

echo "5/5 sign"
if [ ! -f keystore.jks ]; then
  keytool -genkeypair -keystore keystore.jks -alias loc \
          -keyalg RSA -keysize 2048 -validity 10000 \
          -storepass gitsloc -keypass gitsloc -dname "CN=gits loc" >/dev/null 2>&1
  echo "   生成密钥库 keystore.jks（不进仓库）"
fi
$BT/zipalign -f 4 build/unsigned.apk build/aligned.apk
$BT/apksigner sign --ks keystore.jks --ks-pass pass:gitsloc --key-pass pass:gitsloc \
    --out gitsloc.apk build/aligned.apk
echo
echo "OK -> $(pwd)/gitsloc.apk  ($(du -h gitsloc.apk | cut -f1))"
