# Mobile production best practices: secure storage, offline-first, biometrics, and CI/CD (2024–2026)

**This is a comprehensive knowledge base for generating world-class production mobile apps across iOS, Android, and cross-platform frameworks.** It covers four interconnected domains: secure storage and keychain management, offline-first architecture, biometric authentication with passkey integration, and code signing CI/CD pipelines. Every section includes production-grade code patterns, security trade-off analysis, and decision matrices to guide framework and architecture choices. The guidance is iOS-first with Android secondary, and reflects platform changes through Android 17 and iOS 26.

---

## 1. Secure storage and keychain best practices

### 1.1 iOS Keychain Services: the foundation of mobile secret storage

The iOS Keychain is a hardware-encrypted SQLite database managed by `securityd`. Items are encrypted with **AES-256-GCM** using two keys: a metadata key (cached for fast queries) and a secret key (requires Secure Enclave round-trip). All CRUD operations flow through four C functions.

**Core CRUD pattern (Swift):**

```swift
import Security

enum KeychainError: Error {
    case itemNotFound, duplicateItem, invalidItemFormat, unexpectedStatus(OSStatus)
}

// UPSERT — handles the common errSecDuplicateItem case
func save(data: Data, service: String, account: String,
          accessible: CFString = kSecAttrAccessibleWhenUnlockedThisDeviceOnly) throws {
    let query: [String: Any] = [
        kSecClass as String:        kSecClassGenericPassword,
        kSecAttrService as String:  service,
        kSecAttrAccount as String:  account,
        kSecValueData as String:    data,
        kSecAttrAccessible as String: accessible
    ]
    let status = SecItemAdd(query as CFDictionary, nil)
    if status == errSecDuplicateItem {
        let attrs: [String: Any] = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        guard updateStatus == errSecSuccess else { throw KeychainError.unexpectedStatus(updateStatus) }
    } else if status != errSecSuccess {
        throw KeychainError.unexpectedStatus(status)
    }
}

// READ
func read(service: String, account: String) throws -> Data {
    let query: [String: Any] = [
        kSecClass as String:       kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
        kSecReturnData as String:  true,
        kSecMatchLimit as String:  kSecMatchLimitOne
    ]
    var result: AnyObject?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess, let data = result as? Data else {
        throw status == errSecItemNotFound ? KeychainError.itemNotFound : KeychainError.unexpectedStatus(status)
    }
    return data
}
```

**kSecAttrAccessible flags — complete reference:**

| Flag | When accessible | Passcode required | Migrates to new device | Production use |
|---|---|---|---|---|
| `WhenPasscodeSetThisDeviceOnly` | While unlocked | Yes (deleted if removed) | No | **Highest security**: biometric-gated secrets, Secure Enclave keys |
| `WhenUnlockedThisDeviceOnly` | While unlocked | No | No | Sensitive tokens, session keys |
| `WhenUnlocked` *(default)* | While unlocked | No | Yes (encrypted backup) | General passwords, user credentials |
| `AfterFirstUnlockThisDeviceOnly` | After first unlock until restart | No | No | Background-accessible push tokens, VPN creds |
| `AfterFirstUnlock` | After first unlock until restart | No | Yes | Background networking tokens |

The critical behavioral distinction: `WhenUnlocked` items become inaccessible the moment the device locks, while `AfterFirstUnlock` items remain accessible until the next reboot. Background tasks (push notification handlers, BGTaskScheduler work) **require** `AfterFirstUnlock` variants.

**Keychain sharing between apps** uses access groups configured via the Keychain Sharing capability. The group format is `<TeamID>.<group-name>`. Any app signed with the same Team ID and matching group entitlement can read/write shared items:

```swift
let query: [String: Any] = [
    kSecClass as String:           kSecClassGenericPassword,
    kSecAttrService as String:     "com.myapp.auth",
    kSecAttrAccount as String:     "sharedToken",
    kSecAttrAccessGroup as String: "ABC1234DEF.com.mycompany.shared",
    kSecValueData as String:       tokenData
]
```

### 1.2 Secure Enclave and CryptoKit for hardware-bound cryptography

The Secure Enclave is a hardware-isolated coprocessor (A7+ chips) where **private keys never leave** the silicon. It supports only **P-256 (secp256r1)** elliptic curve keys. CryptoKit provides the modern Swift API:

```swift
import CryptoKit

// Create hardware-bound signing key
guard SecureEnclave.isAvailable else { fatalError("SE not available") }
let privateKey = try SecureEnclave.P256.Signing.PrivateKey()
let keyData = privateKey.dataRepresentation  // Store in Keychain for reconstruction

// Sign data — biometric prompt triggers if key has access control
let signature = try privateKey.signature(for: "transaction-payload".data(using: .utf8)!)

// AES-GCM encryption with derived key
let sharedSecret = try privateKey.sharedSecretFromKeyAgreement(with: otherPublicKey)
let derivedKey = sharedSecret.hkdfDerivedSymmetricKey(
    using: SHA256.self, salt: "MyApp-v1".data(using: .utf8)!,
    sharedInfo: "encryption".data(using: .utf8)!, outputByteCount: 32
)
let sealed = try AES.GCM.seal(plaintext, using: derivedKey)
```

For lower-level Secure Enclave operations with biometric binding via the Security framework:

```swift
var error: Unmanaged<CFError>?
guard let access = SecAccessControlCreateWithFlags(
    kCFAllocatorDefault,
    kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    [.privateKeyUsage, .biometryAny],
    &error
) else { throw error!.takeRetainedValue() as Error }

let attrs: [String: Any] = [
    kSecAttrKeyType as String:       kSecAttrKeyTypeECSECPrimeRandom,
    kSecAttrKeySizeInBits as String: 256,
    kSecAttrTokenID as String:       kSecAttrTokenIDSecureEnclave,
    kSecPrivateKeyAttrs as String: [
        kSecAttrIsPermanent as String:    true,
        kSecAttrApplicationTag as String: "com.app.signing".data(using: .utf8)!,
        kSecAttrAccessControl as String:  access
    ]
]
let privateKey = SecKeyCreateRandomKey(attrs as CFDictionary, &error)!
```

**CryptoKit algorithm coverage:** P256/P384/P521 signing and key agreement, Curve25519 (Ed25519 signing, X25519 ECDH), AES-GCM and ChaChaPoly symmetric encryption, SHA256/384/512 hashing, HMAC, and HKDF key derivation.

### 1.3 Android Keystore: hardware-backed keys and StrongBox

Android's Keystore operates across three security tiers: **software** (extractable on rooted devices), **TEE** (isolated area of main processor, key material never leaves), and **StrongBox** (dedicated HSM chip like Titan M — tamper-resistant, side-channel resistant, API 28+). Android 12 replaced Keymaster HAL with **KeyMint HAL** (Rust-based `keystore2` daemon).

```kotlin
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties

fun generateSecureKey(alias: String): SecretKey {
    val spec = KeyGenParameterSpec.Builder(alias,
        KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setKeySize(256)
        .setUserAuthenticationRequired(true)
        .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)  // auth-per-use
        .setInvalidatedByBiometricEnrollment(true)  // key dies if new biometric enrolled
        .setIsStrongBoxBacked(true)  // request SE; catch StrongBoxUnavailableException
        .setUnlockedDeviceRequired(true)
        .build()

    return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        .apply { init(spec) }.generateKey()
}
```

**EncryptedSharedPreferences deprecation notice:** `androidx.security:security-crypto` was deprecated at version 1.1.0-alpha07 (April 2025). Google has not provided a direct replacement. For new apps, use direct Keystore + AES-GCM encryption or the community fork `dev.spght:encryptedprefs-core`. Auto-backup pitfall: encrypted prefs files **must** be excluded from backup — restoring on a new device crashes because the master key is device-bound.

**Key attestation** (API 24+) lets a server cryptographically verify that a key pair lives in hardware. Generate an attested key with `setAttestationChallenge(serverNonce)`, retrieve the certificate chain, and validate it server-side against Google's attestation root. As of **February 2026**, Google is rotating to a new ECDSA P-384 root certificate — developers using direct key attestation must update.

**Play Integrity API** replaced SafetyNet (fully sunset May 2025). It returns three verdict levels: `MEETS_BASIC_INTEGRITY`, `MEETS_DEVICE_INTEGRITY`, and `MEETS_STRONG_INTEGRITY` (requires hardware-backed boot verification + security update within last year on Android 13+). Since December 2024 it uses Android Platform Key Attestation under the hood on Android 13+.

### 1.4 Cross-platform secure storage abstractions

**flutter_secure_storage** (v10.0.0): Uses iOS Keychain natively and custom RSA OAEP + AES-GCM on Android with keys in Android Keystore. Supports biometric gating. Key limitations: iOS Keychain data persists across reinstalls (requires first-launch cleanup), web implementation stores keys in `localStorage` (not secure), and emulators may lack hardware-backed keystore.

**react-native-keychain**: Wraps iOS Keychain and Android CipherStorage. Supports biometric access control. Security concern: historically used deprecated `facebook-conceal` library, and Android defaults to AES-CBC instead of AES-GCM. **expo-secure-store**: Uses Keychain on iOS and encrypted SharedPreferences on Android. Limitations include a ~2048 byte iOS limit and shared keychain access across same-team apps by default.

**Kotlin Multiplatform** uses the `expect/actual` pattern for platform-native implementations. Existing libraries: **KVault** (Liftric), **Multiplatform Settings** (Touchlab), and **KSafe** (AES-256-GCM with root detection). The key advantage of KMP is that `actual` implementations call native APIs directly with zero abstraction penalty.

**Token storage decision matrix:**

| Token type | Storage | Protection | Notes |
|---|---|---|---|
| Access token (5–60 min) | Keychain / Keystore | `WhenUnlockedThisDeviceOnly` | Short TTL reduces risk |
| Refresh token (7–30 days) | Keychain with biometric gate | `WhenPasscodeSetThisDeviceOnly` + `.userPresence` | Rotate on each use |
| API keys | Fetch from server → Keychain | `AfterFirstUnlockThisDeviceOnly` | **Never hardcode in source** |
| Signing keys (high security) | Secure Enclave / StrongBox | Hardware-bound, non-exportable | Use CryptoKit `SecureEnclave.P256` |

### 1.5 Common pitfalls that break production security

**Keychain persistence across reinstalls** is the single most common surprise. Items survive app deletion. Fix with a `UserDefaults` flag:

```swift
if !UserDefaults.standard.bool(forKey: "hasRunBefore") {
    [kSecClassGenericPassword, kSecClassKey, kSecClassCertificate, kSecClassIdentity]
        .forEach { SecItemDelete([kSecClass as String: $0] as CFDictionary) }
    UserDefaults.standard.set(true, forKey: "hasRunBefore")
}
```

**Backup vulnerabilities:** `ThisDeviceOnly` items never migrate to a different device regardless of backup type. `WhenPasscodeSetThisDeviceOnly` items are deleted if the passcode is removed. Only items with `kSecAttrSynchronizable = true` sync via iCloud Keychain (disabled by default). On Android, **`KeyPermanentlyInvalidatedException`** is thrown when a biometric enrollment changes and `setInvalidatedByBiometricEnrollment(true)` was set — apps must handle this with a re-enrollment flow.

**Root/jailbreak detection** is fundamentally an arms race. Tools like Magisk Hide and Zygisk actively conceal root artifacts. Client-side detection should be one layer in defense-in-depth, combined with server-side attestation (Play Integrity / App Attest). Secure Enclave keys remain protected even on jailbroken iOS devices.

---

## 2. Offline-first architecture patterns

### 2.1 iOS storage: SwiftData, Core Data + CloudKit, and GRDB

**SwiftData** (iOS 17+) is Apple's modern persistence layer built on Core Data/SQLite with the `@Model` macro. CloudKit sync is automatic with zero code once capabilities are configured, but imposes constraints: all properties must have default values or be optional, all relationships optional, and no `@Attribute(.unique)`.

**Core Data + CloudKit** via `NSPersistentCloudKitContainer` remains the workhorse for complex iOS apps. The new **CKSyncEngine** (iOS 17+) provides a higher-level API replacing manual CloudKit sync — used by Apple's Freeform app. It handles push notifications, change tokens, and zone setup automatically.

**GRDB.swift** is the best SQLite wrapper for custom offline storage with WAL mode, FTS5, and observation patterns — ideal when building a custom sync engine rather than relying on CloudKit.

**Realm Swift** historically offered Realm Sync (now MongoDB Atlas Device Sync), but **Atlas Device Sync was deprecated in September 2024 with EOL September 2025**. PowerSync is the recommended migration path.

### 2.2 Android storage: Room + WorkManager as the canonical stack

Room + WorkManager is the officially recommended Android offline-first pattern. The local database is the single source of truth; UI observes `Flow<List<T>>` from Room DAOs; writes go to Room as PENDING; WorkManager syncs in the background.

```kotlin
@Entity(tableName = "notes")
data class Note(
    @PrimaryKey val id: String,
    val title: String,
    val body: String,
    val syncStatus: SyncStatus = SyncStatus.PENDING,
    val updatedAt: Long = System.currentTimeMillis()
)

class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val unsynced = noteDao.getUnsyncedNotes()
        unsynced.forEach { note ->
            try {
                api.uploadNote(note)
                noteDao.insert(note.copy(syncStatus = SyncStatus.SYNCED))
            } catch (e: Exception) { return Result.retry() }
        }
        return Result.success()
    }
}

// Schedule periodic sync
val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
    .setConstraints(Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED).build())
    .build()
WorkManager.getInstance(context).enqueueUniquePeriodicWork("sync", ExistingPeriodicWorkPolicy.KEEP, request)
```

**SQLDelight** (KMP-compatible) generates typesafe Kotlin APIs from SQL statements, making it ideal for shared offline data layers across iOS and Android.

### 2.3 Cross-platform solutions and the 2025 sync landscape

**WatermelonDB** (React Native) provides lazy-loading SQLite with a built-in sync protocol using `pullChanges`/`pushChanges`. **Drift** (Flutter) offers compile-time SQL verification with stream-based reactive queries. **Firebase Firestore** offline persistence is enabled by default on iOS/Android — it caches documents locally, queues writes while offline, and uses LWW conflict resolution. Default cache is 100MB (configurable to unlimited).

**PowerSync** has emerged as the leading managed sync solution for Postgres-backed apps. It taps into Postgres logical replication (WAL) to stream changes, uses local SQLite for reads/writes, and maintains an upload queue for mutations. SDKs exist for Flutter, React Native, and Web, with Swift and Kotlin SDKs in development. **ElectricSQL** provides Postgres-to-client sync but explicitly does not handle client-side persistence — developers implement their own.

### 2.4 Conflict resolution: when to use which strategy

**Last-write-wins (LWW)** is the simplest approach — each record carries a timestamp, highest wins. Used by Firestore offline mode and most custom sync implementations. **Silent data loss** is the trade-off. Best for single-user-per-device scenarios.

**CRDTs** (Automerge, Yjs) enable automatic conflict-free merging without coordination. **Automerge** provides JSON-like document CRDTs with complete history but growing file sizes. **Yjs** excels at text-heavy collaborative editing with a modular architecture. CRDTs add significant complexity; use them only when multiple users simultaneously edit highly visible, granular data.

**Vector clocks** detect conflicts without resolving them, allowing app-specific merge logic. **Hybrid Logical Clocks (HLCs)** combine physical and logical timestamps in 64 bits — a practical alternative to full vector clocks.

| Strategy | Best for | Complexity | Data loss risk |
|---|---|---|---|
| LWW | Single-user, settings, non-collaborative | Low | Medium (silent overwrites) |
| Vector clocks | Multi-device with conflict UI | Medium | Low (detects conflicts) |
| CRDTs | Collaborative editing, multiplayer | High | Low (auto-merge) |
| Custom merge | Domain-specific business rules | Medium-High | Low |

**Rule of thumb:** Start with LWW. Upgrade to vector clocks + user-facing conflict UI if concurrent edits to the same entity are frequent. Reserve CRDTs for collaborative editing.

### 2.5 Background sync: BGTaskScheduler and WorkManager

**iOS BGTaskScheduler** offers two task types: `BGAppRefreshTask` (~30 seconds, quick data refresh) and `BGProcessingTask` (minutes-long maintenance). The system decides actual execution time based on battery, usage patterns, and network. Register handlers at app launch, schedule when entering background, and always re-schedule inside the handler:

```swift
BGTaskScheduler.shared.register(forTaskWithIdentifier: "com.app.sync", using: nil) { task in
    self.handleSync(task as! BGAppRefreshTask)
}

func scheduleSync() {
    let request = BGAppRefreshTaskRequest(identifier: "com.app.sync")
    request.earliestBeginDate = Date(timeIntervalSinceNow: 3600)
    try? BGTaskScheduler.shared.submit(request)
}
```

**Android WorkManager** guarantees execution across app kills and device restarts. It supports constraint-based scheduling (`NetworkType.CONNECTED`, `requiresBatteryNotLow`), exponential backoff, work chaining, and expedited work (Android 12+). Return `Result.success()`, `Result.retry()` (triggers backoff), or `Result.failure()`.

**Network reachability** uses `NWPathMonitor` on iOS and `ConnectivityManager.registerNetworkCallback` on Android. Cross-platform: `connectivity_plus` (Flutter) and `@react-native-community/netinfo` (React Native).

### 2.6 Queue-based architectures for offline mutations

The pending operations table pattern stores an outbox in the same local database as app data. **UI writes and outbox enqueues must happen in the same database transaction** — if separate, the UI can show data that never syncs.

```kotlin
@Entity(tableName = "pending_operations")
data class PendingOperation(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    val idempotencyKey: String,          // Client-generated UUID for deduplication
    val operationType: String,           // CREATE, UPDATE, DELETE
    val entityType: String,
    val entityId: String,
    val payload: String,                 // JSON
    val status: OperationStatus = OperationStatus.PENDING,
    val retryCount: Int = 0,
    val maxRetries: Int = 5
)
```

**Idempotency keys** solve the "request succeeded but app never got the response" problem. The client generates a UUID per mutation; the server stores key + outcome and returns cached results for duplicate keys. Rollback strategies include automatic state reversion, conflict UI (show both versions), event-sourcing inverse operations, or flag-and-defer (mark as CONFLICTED, resolve at next interaction).

---

## 3. Biometric authentication and passkey integration

### 3.1 iOS LocalAuthentication: the critical difference between gating and binding

The **most important security principle** for biometrics: using `LAContext.evaluatePolicy()` alone is vulnerable — an attacker can hook it via Frida to return `true`. **Keychain-bound biometric authentication** provides true cryptographic binding that cannot be bypassed at the application layer.

**LAPolicy options:**
- `.deviceOwnerAuthenticationWithBiometrics`: Biometric only, no passcode fallback. Fails completely if biometrics locked out.
- `.deviceOwnerAuthentication`: Biometric with automatic passcode fallback. Recommended for most apps.

**SecAccessControlCreateFlags for keychain binding:**

| Flag | Behavior | Invalidates on bio change |
|---|---|---|
| `.biometryCurrentSet` | Requires currently enrolled biometrics | Yes — item becomes inaccessible |
| `.biometryAny` | Any enrolled biometrics including new | No |
| `.userPresence` | Biometric or device passcode fallback | No |

**Production pattern — biometric-bound keychain item (iOS):**

```swift
func storeBiometricProtected(key: String, data: Data) -> OSStatus {
    var error: Unmanaged<CFError>?
    guard let accessControl = SecAccessControlCreateWithFlags(
        kCFAllocatorDefault,
        kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
        .biometryCurrentSet,
        &error
    ) else { return errSecParam }

    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrAccount as String: key,
        kSecAttrAccessControl as String: accessControl,
        kSecValueData as String: data
    ]
    return SecItemAdd(query as CFDictionary, nil)
}
```

Key notes: `kSecAttrAccessControl` is mutually exclusive with `kSecAttrAccessible`. Pass an authenticated `LAContext` via `kSecUseAuthenticationContext` to reuse a biometric result. Face ID requires `NSFaceIDUsageDescription` in Info.plist. Detect enrollment changes via `evaluatedPolicyDomainState`.

### 3.2 Android BiometricPrompt with CryptoObject — hardware-enforced authentication

**Without CryptoObject, BiometricPrompt is just a bypassable UI gate.** The secure pattern generates a key in Android Keystore that requires biometric unlock, then passes the key's cipher as a `CryptoObject`:

```kotlin
// Generate biometric-bound key
val keyGen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
keyGen.init(KeyGenParameterSpec.Builder("BiometricKey",
    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
    .setUserAuthenticationRequired(true)
    .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)  // auth-per-use
    .setInvalidatedByBiometricEnrollment(true)
    .build())
keyGen.generateKey()

// Authenticate with CryptoObject
val cipher = Cipher.getInstance("AES/GCM/NoPadding")
val key = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.getKey("BiometricKey", null)
cipher.init(Cipher.ENCRYPT_MODE, key as SecretKey)

val prompt = BiometricPrompt(activity, executor, object : BiometricPrompt.AuthenticationCallback() {
    override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
        val authedCipher = result.cryptoObject?.cipher!!
        val ciphertext = authedCipher.doFinal(plaintext.toByteArray())  // Hardware-verified
    }
})
prompt.authenticate(promptInfo, BiometricPrompt.CryptoObject(cipher))
```

**Authenticator types:** `BIOMETRIC_STRONG` (Class 3, hardware TEE/SE, supports CryptoObject) for banking/enterprise; `BIOMETRIC_WEAK` (Class 2, software OK, no CryptoObject) for low-risk; `DEVICE_CREDENTIAL` (PIN/pattern) for universal fallback. When combining `BIOMETRIC_STRONG or DEVICE_CREDENTIAL` as allowed authenticators, you cannot set a negative button text and cannot use CryptoObject with auth-per-use keys.

**Android 15 changes:** Single-tap passkey sign-in merges account selection + biometric into one screen. Auto-deletion of poorly performing biometric models. `BiometricPromptData` for single-tap credential providers.

### 3.3 Cross-platform biometric libraries: security limitations matter

**Flutter `local_auth`**: Provides boolean-result gating only — **not secure for authentication**. For crypto binding, use `biometric_storage` (CryptoObject on Android, SecAccessControl with `.biometryCurrentSet` on iOS) or `biometric_signature` (hardware-backed signatures via Secure Enclave/StrongBox).

**React Native `react-native-biometrics`** offers two methods: `simplePrompt()` (boolean, insecure) and `createSignature()` (secure — generates RSA 2048 keypair in native keystore, private key requires biometric to sign). NVISO Labs security audit confirmed `createSignature()` is properly secure:

```javascript
// Registration — public key to server
const { publicKey } = await rnBiometrics.createKeys();
await sendPublicKeyToServer(publicKey);

// Authentication — sign challenge with biometric-unlocked key
const { signature } = await rnBiometrics.createSignature({
    promptMessage: 'Sign in', payload: `${Date.now()}|${userId}`
});
await verifySignatureOnServer(signature, payload);
```

**KMP** uses `expect/actual` with BiometricPrompt + AndroidKeyStore on Android and LAContext + Keychain on iOS via Kotlin/Native interop. **Rule: if your biometric flow doesn't involve a cryptographic operation that cannot be performed without the biometric, it is NOT secure.**

### 3.4 Passkeys and FIDO2: the new authentication paradigm (2025–2026)

Passkey adoption has reached critical mass: **69% of consumers have at least one passkey**, **93% authentication success rate** vs 63% for legacy methods, and **87% of businesses** have deployed or are deploying passkeys. Regulatory mandates are accelerating adoption — UAE eliminated SMS OTP by March 2026, India RBI deadline April 2026, NIST SP 800-63-4 (July 2025) requires phishing-resistant options at AAL2.

**Apple Passkeys** via `ASAuthorizationPlatformPublicKeyCredentialProvider`:

```swift
import AuthenticationServices

func registerPasskey(username: String, userID: Data, challenge: Data) {
    let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(
        relyingPartyIdentifier: "example.com")
    let request = provider.createCredentialRegistrationRequest(
        challenge: challenge, name: username, userID: userID)
    let controller = ASAuthorizationController(authorizationRequests: [request])
    controller.delegate = self
    controller.presentationContextProvider = self
    controller.performRequests()
}
```

**iOS 26 enhancements:** Passkey Account Creation API for one-step sign-up, automatic passkey upgrades with `requestStyle: .conditional`, CXF support for cross-provider transfer.

**Google Credential Manager** is the unified Android API (14+, backported to 4.4+):

```kotlin
suspend fun createPasskey(context: Context, requestJson: String) {
    val credentialManager = CredentialManager.create(context)
    val request = CreatePublicKeyCredentialRequest(requestJson)
    val result = credentialManager.createCredential(context = context, request = request)
}
```

**Cross-platform interoperability** relies on QR-based FIDO Cross-Device Authentication for cross-ecosystem scenarios. CXF/CXP standards for passkey migration between providers are emerging but not yet finalized. Passkeys combine two authentication factors in one action: possession (device with private key) + inherence (biometric to unlock key).

**Authentication strategy decision tree:**

- **New consumer app:** Primary passkeys, password + MFA fallback during transition
- **Banking/health/enterprise:** Device-bound passkeys (AAL3), CryptoObject-bound biometric for high-risk ops, server-verified challenge-response
- **Existing app migration:** Phase 1: add passkey creation on successful login → Phase 2: automatic upgrades → Phase 3: passkeys default → Phase 4: deprecate passwords
- **Cross-platform (Flutter/RN):** Use native modules for passkeys (no mature cross-platform passkey libs yet), crypto-binding libs for biometrics

---

## 4. Code signing pipelines and CI/CD

### 4.1 iOS code signing: certificates, profiles, and Fastlane match

iOS code signing requires a **certificate** (developer's identity, contains public key paired with local private key) and a **provisioning profile** (bundles certificate + App ID + entitlements + device UDIDs). Four profile types exist: Development (debug on registered devices), Ad Hoc (distribute to limited devices), App Store (TestFlight/App Store submission), and Enterprise (internal distribution).

**Automatic vs manual signing:** Use automatic for local development (zero friction, auto-repairs), **manual for CI/CD and distribution** (deterministic, no interactive login required).

**Fastlane match** implements centralized certificate management — one signing identity shared across the team, stored encrypted (AES-256) in a Git repo, S3, or GCS:

```ruby
# Matchfile
git_url("git@github.com:yourcompany/ios-certificates.git")
storage_mode("git")
type("appstore")
app_identifier(["com.example.app"])

# CI usage — ALWAYS readonly
platform :ios do
  before_all do
    setup_ci  # Creates fastlane_tmp_keychain
  end
  lane :build do
    app_store_connect_api_key(
      key_id: ENV["ASC_KEY_ID"], issuer_id: ENV["ASC_ISSUER_ID"],
      key_content: ENV["ASC_KEY_CONTENT"], is_key_content_base64: true
    )
    match(type: "appstore", readonly: true)  # CRITICAL: readonly on CI
    gym(scheme: "MyApp", export_method: "app-store")
    upload_to_testflight(skip_waiting_for_build_processing: true)
  end
end
```

**App Store Connect API** uses JWT-based authentication with API Keys, eliminating 2FA issues. Download the `.p8` key file (one-time), note Key ID and Issuer ID, and base64-encode for CI secrets.

### 4.2 Android signing: Play App Signing and signature schemes

Play App Signing (mandatory since August 2021) separates the **upload key** (developer-managed) from the **app signing key** (Google-managed). If the upload key is lost, it can be reset via Play Console. The app signing key can be upgraded via `apksigner rotate` (Android 13+ for full support).

```kotlin
// build.gradle.kts
android {
    signingConfigs {
        create("release") {
            if (System.getenv("CI") != null) {
                storeFile = file(System.getenv("KEYSTORE_PATH") ?: "release.jks")
                storePassword = System.getenv("SIGNING_STORE_PASSWORD")
                keyAlias = System.getenv("SIGNING_KEY_ALIAS")
                keyPassword = System.getenv("SIGNING_KEY_PASSWORD")
            } else {
                val props = Properties().apply { load(rootProject.file("key.properties").inputStream()) }
                keyAlias = props["keyAlias"] as String
                storeFile = file(props["storeFile"] as String)
                // ...
            }
        }
    }
}
```

**Signature schemes:** V2 (whole-APK signing, required by Play Store) and V3 (extends V2, enables key rotation) are the production standards. V4 (Merkle hash tree) enables incremental installation. V1 (JAR signing) is legacy — include only for devices below API 24.

### 4.3 GitHub Actions workflows for iOS and Android

**iOS workflow with Fastlane:**

```yaml
name: iOS Build & Deploy
on:
  push:
    branches: [main]
jobs:
  build-ios:
    runs-on: macos-latest  # ~$0.08/min, 10x Linux cost
    steps:
      - uses: actions/checkout@v4
      - uses: ruby/setup-ruby@v1
        with: { ruby-version: '3.2', bundler-cache: true }
      - name: Build and deploy
        env:
          MATCH_PASSWORD: ${{ secrets.MATCH_PASSWORD }}
          MATCH_GIT_BASIC_AUTHORIZATION: ${{ secrets.MATCH_GIT_BASIC_AUTHORIZATION }}
          ASC_KEY_ID: ${{ secrets.ASC_KEY_ID }}
          ASC_ISSUER_ID: ${{ secrets.ASC_ISSUER_ID }}
          ASC_KEY_CONTENT: ${{ secrets.ASC_KEY_CONTENT }}
        run: bundle exec fastlane beta
```

**Android workflow:**

```yaml
jobs:
  build-android:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with: { distribution: 'temurin', java-version: '17' }
      - name: Decode keystore
        run: echo "${{ secrets.SIGNING_KEY_STORE_BASE64 }}" | base64 -d > app/release.jks
      - name: Build
        env:
          SIGNING_KEY_ALIAS: ${{ secrets.SIGNING_KEY_ALIAS }}
          SIGNING_KEY_PASSWORD: ${{ secrets.SIGNING_KEY_PASSWORD }}
          SIGNING_STORE_PASSWORD: ${{ secrets.SIGNING_STORE_PASSWORD }}
        run: ./gradlew bundleRelease
```

**Xcode Cloud** (25 free hours/month) offers seamless Xcode integration with automatic signing and TestFlight distribution, but is Apple-only with no Fastlane integration. **41% of iOS developers** use Xcode Cloud vs 31% using GitHub Actions.

### 4.4 Secrets management: from CI variables to enterprise vaults

**Base64-encoded certificates** are the standard pattern for CI — binary `.p12`/`.jks`/`.mobileprovision` files are base64-encoded, stored as secrets, and decoded at build time. Always create a **temporary keychain** on macOS CI runners (Fastlane's `setup_ci` creates `fastlane_tmp_keychain`), and always clean up in a post-step.

For enterprise environments, **HashiCorp Vault** provides dynamic secrets, encryption-as-a-service, and fine-grained ACL policies:

```yaml
- uses: hashicorp/vault-action@v3
  with:
    url: https://vault.example.com
    method: jwt
    role: ci-role
    secrets: |
      secret/data/ios/signing MATCH_PASSWORD | MATCH_PASSWORD;
      secret/data/android/signing KEYSTORE | KEYSTORE_BASE64;
```

**Best practices:** Short-lived scoped credentials for CI vault access, token rotation after each job, least-privilege secret paths, audit logging, offline backup of signing materials, annual rotation of `MATCH_PASSWORD`.

---

## 5. Framework selection for security-critical apps

### 5.1 Decision matrix: native vs Flutter vs React Native vs KMP

| Factor | Native (Swift/Kotlin) | Flutter | React Native | KMP |
|---|---|---|---|---|
| Security API access | Full, direct | Via channels/FFI | Via JSI/TurboModules | Full via expect/actual |
| Reverse engineering resistance | Highest | High (ARM code) | Moderate (Hermes bytecode) | High (native binaries) |
| Secure Enclave/Keystore | Direct | Via plugins | Via plugins | Direct |
| Crypto performance | Best | Good (FFI for heavy ops) | Good (JSI) | Near-native |
| Time-to-market | Slowest (2x codebases) | Fast | Fast | Moderate |
| Best for | Banking, government, DRM | Consumer fintech, MVPs | Apps with web teams | Enterprise fintech, healthtech |

**When native is the only option:** Direct Secure Enclave operations, hardware-backed attestation, FIPS 140-2/3 compliance, real-time anti-tampering/RASP, DRM with FairPlay/Widevine L1.

**KMP has surged** from 7% to ~20% adoption among surveyed developers (2024→2025), used by Netflix, Cash App, Google Workspace, Airbnb, and Duolingo. Compose Multiplatform reached **Stable** on iOS (1.8.0, May 2025). The `expect/actual` pattern provides full native security API access with shared business logic — a strong option for security-critical apps that need cross-platform efficiency.

**Flutter's** `flutter_secure_storage` v10.0.0 uses custom ciphers rather than deprecated Jetpack Security. Nubank (Latin America's largest digital bank) uses Flutter in production. **React Native's** New Architecture (Fabric + TurboModules + JSI) eliminates the old JSON bridge bottleneck, but Hermes bytecode is more reversible than compiled code.

### 5.2 Platform developments shaping 2025–2026

**Android 17** introduces the first phase of **post-quantum cryptography**: Android Keystore natively supports ML-DSA (FIPS 204), Android Verified Boot updated with ML-DSA quantum-resistant signatures, and Play App Signing generates quantum-safe ML-DSA signing keys for new apps. **Migration guidance:** inventory all cryptographic usage, use Google's Tink library for crypto abstraction, begin testing ML-DSA on Android 17 beta, and implement hybrid classical + PQC approaches.

**Swift 6.2** (September 2025) introduced "Approachable Concurrency" — single-threaded by default with `@concurrent` for explicit parallelism. Impact on security: `actor` isolation protects shared mutable security state, and `InlineArray`/`Span` types enable safe low-level memory access for crypto implementations.

**Apple iMessage PQ3** (2024) already uses post-quantum cryptography. No public Apple CryptoKit PQC timeline, but additions are expected at WWDC 2026.

## Conclusion

The mobile security landscape in 2025–2026 is defined by three converging trends. First, **hardware-backed cryptography is now table stakes** — Secure Enclave and StrongBox/TEE should gate all high-value secrets, not software-only abstractions. The deprecation of `EncryptedSharedPreferences` and the shift to Remote Key Provisioning on Android 16 reinforce this. Second, **passkeys are replacing passwords** at regulatory speed — with NIST, UAE, India, and EU all mandating phishing-resistant authentication, apps launched in 2026 should treat passkeys as the primary authentication method from day one. Third, **post-quantum cryptography is arriving on mobile** through Android 17's ML-DSA support, and apps should begin cryptographic inventory and hybrid migration planning now.

For an AI coding agent generating production apps, the critical architectural decisions come down to: always use cryptographic biometric binding (never boolean gating), always use `ThisDeviceOnly` keychain accessibility for sensitive data, always implement idempotency keys in offline mutation queues, always use `readonly: true` for Fastlane match on CI, and always prefer hardware-backed key storage over software encryption. The gap between "works in development" and "secure in production" is almost entirely in these details.