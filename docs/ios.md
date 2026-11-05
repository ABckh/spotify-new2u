# The iPhone app

The app, "New to You", talks to one server, and its address is fixed when the
app is built. There is no App Store build: you build the app for your own
server and install it on your phone from your Mac.

You need a Mac with a recent Xcode (the project is set up with Xcode 27) and an
iPhone on iOS 17 or later.

## Install it on your iPhone

1. Sign Xcode in to your Apple ID: Xcode → Settings → Accounts → + → Apple ID.
   A free Apple ID gives you a Personal Team, which is enough.
2. Make your signing file. It is git-ignored; never commit it.

   ```sh
   cp ios/Signing.local.xcconfig.example ios/Signing.local.xcconfig
   ```

   Put your team's 10-character ID in `DEVELOPMENT_TEAM` (Xcode → Settings →
   Accounts → your team). If Xcode later says the bundle ID is taken, change
   `PRODUCT_BUNDLE_IDENTIFIER` to any reverse-DNS name of your own.
3. On the iPhone, turn on Developer Mode (Settings → Privacy & Security →
   Developer Mode), plug it in, unlock it, and tap **Trust** if it asks.
4. Build and install, with your server's address (`API_HOSTNAME` on the
   server, https only):

   ```sh
   API_BASE_URL=https://api.example.org scripts/ios-install-device.sh
   ```

5. The first time only: on the iPhone, open Settings → General → VPN & Device
   Management → your Apple ID → Trust. Then open New to You.

With a free Apple ID the app stops opening after 7 days; run the script again
to renew it. A paid Apple Developer membership makes an install last a year.

## The server side

The app finishes sign-in on its own URL scheme, `new2u://signed-in`. Set the
server's `APP_CALLBACK_URL` to exactly that. The server also needs your
Spotify app and the allowed accounts (see
[Create the Spotify app](self-hosting.md#create-the-spotify-app)).

## In the Simulator

Open `ios/New2U.xcodeproj` and run the `New2U` scheme. A Debug build talks to
`http://127.0.0.1:8000`, the server `docker compose up` runs on your Mac (see
[Development](development.md)). For sign-in to come back to the app there,
set `APP_CALLBACK_URL=new2u://signed-in` in `.env` as well.

Debug builds can also run on built-in sample data, without a server: launch
with `-fixture <name>` (the names are in `ios/New2U/DebugFixtures.swift`).

The tests:

```sh
xcodebuild test -project ios/New2U.xcodeproj -scheme New2U \
  -destination 'platform=iOS Simulator,name=iPhone 17' -parallel-testing-enabled NO
```

Use any Simulator that `xcrun simctl list devices available` shows.
