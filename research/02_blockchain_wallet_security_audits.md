# How blockchain security firms audit crypto wallets

**The core finding: wallet security audits span six distinct domains—key lifecycle management, cryptographic implementation, transaction integrity, client-side hardening, supply chain verification, and network-layer defenses.** Major firms like Trail of Bits, OpenZeppelin, CertiK, Halborn, SlowMist, and Least Authority have published checklists, frameworks, and hundreds of public audit reports that collectively define the industry standard. For a software engineer building blockchain fundamentals, understanding these audit categories reveals exactly where ECDSA, Ed25519, BIP-32 HD derivation, and UTXO/account models create exploitable attack surfaces in practice.

This report covers what auditors actually check, the vulnerability classes they hunt, public resources you can study, and how smart contract wallets (Safe, Argent, ERC-4337) differ fundamentally from traditional key-management wallets (MetaMask, Ledger, Trezor) and MPC wallets (Fireblocks, ZenGo).

---

## The six pillars of a wallet security audit

Every major audit firm organizes wallet assessments around overlapping but distinct categories. SlowMist publishes the most granular framework, available at their [wallet security audit service page](https://www.slowmist.com/service-wallet-security-audit.html), while CertiK maintains the most detailed public checklist at their [wallet security assessment checklist](https://www.certik.com/resources/blog/cryptowalletsecurityassessmentchecklist). Synthesizing across firms, the canonical audit categories are:

**Key lifecycle management** covers generation, storage, usage, backup, and destruction. Auditors verify entropy source quality for BIP-39 mnemonic generation, confirm BIP-32/44 HD derivation path correctness, check that keys are encrypted at rest (and that the encryption is non-trivial—Least Authority found Rabby Wallet's password-encrypted storage provided only **~40.54 bits of effective security**, making brute-force feasible despite PBKDF2 stretching), verify memory zeroization after signing operations, and confirm that seed backup flows force user verification. Trail of Bits' "[10 Rules for the Secure Use of Cryptocurrency Hardware Wallets](https://blog.trailofbits.com/2018/11/27/10-rules-for-the-secure-use-of-cryptocurrency-hardware-wallets/)" remains the definitive hardware wallet security checklist—rules include never using pre-initialized devices, testing recovery words on a spare device, storing seeds in tamper-evident envelopes separate from the device, and verifying multi-signature address generation manually.

**Cryptographic implementation** auditing verifies RFC 6979 deterministic nonce generation for ECDSA, checks for nonce bias that enables lattice attacks, validates point-on-curve checks for ECDH, and confirms constant-time algorithms to prevent side-channel leakage. **Transaction security** covers replay protection (EIP-155 chain ID binding), signature malleability prevention, nonce management (Ethereum account model), UTXO selection privacy (Bitcoin), and "What You See Is What You Sign" (WYSIWYS) guarantees. **Client-side security** encompasses browser extension data persistence, screenshot/screen-recording prevention, clipboard monitoring defenses, root/jailbreak detection, and background app state obfuscation—all explicitly in SlowMist's checklist. **Supply chain verification** audits dependency integrity, build reproducibility, and code signing. **Network-layer defenses** verify RPC endpoint authenticity, TLS enforcement, and resistance to eclipse attacks.

The CryptoCurrency Security Standard (CCSS), maintained by the CryptoCurrency Certification Consortium, formalizes these into **10 security aspects with 41 aspect controls** across three certification levels. Fireblocks was the first entity to achieve CCSS-QSP Level 3 certification. The standard covers key/seed generation, wallet creation, key storage, key usage, compromise policy, keyholder grant/revoke policies, third-party audits, data sanitization, proof of reserve, and audit logs. You can review it at [cryptoconsortium.org](https://cryptoconsortium.org/standards-2/).

---

## Cryptographic attack vectors that auditors verify against

The most technically demanding audit work targets the cryptographic primitives themselves. For engineers learning ECC and ECDSA, these vulnerabilities illustrate why implementation details matter as much as mathematical security.

**ECDSA nonce reuse** is the most catastrophic single-point failure. If two signatures share the same nonce `k` (producing identical `r` values), the private key is algebraically recoverable: `k = (s₁ - s₂)⁻¹ · (H(m₁) - H(m₂))`, then `x = r⁻¹ · (k·s - H(m))`. This is the same flaw Sony exploited in the PS3 signing key extraction (2010). Kudelski Security's [Polynonce research (2023)](https://research.kudelskisecurity.com/2023/03/06/polynonce-a-tale-of-a-novel-ecdsa-attack-and-bitcoin-tears/) found **773 vulnerable Bitcoin wallets** with repeated nonces—all already drained. Their work generalized the attack: even *polynomial relationships* between consecutive nonces enable key recovery, not just exact reuse. Trail of Bits' "[ECDSA: Handle with Care](https://blog.trailofbits.com/2020/06/11/ecdsa-handle-with-care/)" demonstrates that lattice attacks on biased nonces can be implemented in under 100 lines of Python. Breitner and Heninger's [2019 paper](https://eprint.iacr.org/2019/023) showed that as few as **4 biased bits** in a 256-bit nonce enable full key recovery on secp256k1. Auditors verify RFC 6979 compliance, test for `r`-value uniqueness across all signatures, and check for timing side channels during nonce generation.

**Ed25519 eliminates nonce reuse by design**—the nonce is deterministically derived from the private key and message hash. This is a fundamental architectural advantage over ECDSA. However, Ed25519 introduces its own subtlety: the curve has **cofactor 8**, which caused a double-spend vulnerability across all CryptoNote currencies (including Monero). Polkadot uses sr25519 (Schnorr over Curve25519 via the Ristretto group) specifically to avoid cofactor issues. For new systems without interoperability constraints, the expert consensus is to choose Ed25519 over secp256k1 for stronger practical security, deterministic signing, and fewer implementation pitfalls.

**BIP-32 HD wallet key leakage** is a design-level vulnerability documented in the standard itself. If an attacker obtains the master extended public key (xpub) *and any non-hardened child private key*, they can recover the master private key: `master_private = child_private - HMAC-SHA512(chain_code, public_key || index)`. Das and Erwig's [2021 formal analysis](https://eprint.iacr.org/2021/1287.pdf) showed BIP-32 achieves only ~94 bits of concrete security—below the 128-bit target. Auditors verify hardened derivation for sensitive paths, ensure chain codes are never exposed alongside child keys, and confirm xpub export doesn't leak to untrusted parties.

**Side-channel attacks on hardware wallets** are physically demonstrated. Ledger Donjon extracted both the user PIN and ECDSA private key from Trezor One's STM32F205 MCU via power consumption analysis during scalar multiplication, using 150,000 profiling traces. They also recovered Ledger Blue PINs from **2 meters away** using electromagnetic emissions captured with a HackRF SDR and classified via TensorFlow. These attacks work because general-purpose microcontrollers lack the constant-time execution, power shaping, and dual-rail logic of dedicated Secure Elements (EAL5+/6+ certified). Kudelski Security explicitly includes side-channel analysis (power consumption and magnetic field emissions) in their [wallet security assessment framework](https://www.kudelski-iot.com/industries/crypto-wallet-security).

---

## Real-world vulnerability classes across hot and cold wallets

Beyond cryptographic primitives, auditors hunt for implementation-level vulnerabilities that have caused actual losses. These divide naturally by wallet type.

**Browser extension wallets** have a unique attack surface. Halborn discovered the "Demonic" vulnerability (CVE-2022-32969), affecting MetaMask, Brave Wallet, Phantom, and xDefi: pressing "Show Secret Recovery Phrase" caused browsers to cache the **BIP-39 mnemonic in plaintext on local disk**, recoverable by anyone with file system access. MetaMask also suffered a clickjacking vulnerability where `web_accessible_resources` configuration exposed internal pages via iframe. OpenZeppelin's research on Gnosis Safe revealed that [modules could be attached during wallet deployment](https://www.openzeppelin.com/news/backdooring-gnosis-safe-multisig-wallets) before owner initialization, giving deployers a permanent backdoor—a vulnerability class specific to smart contract wallets but exploitable through browser extension UIs.

**Mobile wallets** require auditing against the OWASP Mobile Application Security Verification Standard (MASVS), which SlowMist explicitly references. Their checklist includes runtime environment detection (root/jailbreak), screenshot/screen-recording prevention, clipboard monitoring defense, keyboard keystroke cache clearing, background obfuscation, deeplink security, and WebView DOM isolation. CertiK's checklist adds: does the Android app prevent screenshots of sensitive data? Does the iOS app warn against screenshots? Does the app leak sensitive info in background screenshots?

**Hardware wallets** demand white-box audits according to SlowMist, because hardware flaws may be **unfixable via firmware updates**—only new hardware versions can remediate them. Kudelski Security's framework covers security of identification and end-user authentication, confidentiality/integrity/availability of keys and assets, robustness of cryptographic primitive implementation against side-channel attacks, connectivity and communication protocol security, and the full security lifecycle from provisioning through decommissioning. Kudelski is accredited by Ledger as an authorized security audit lab for third-party applications.

**Supply chain attacks** represent a growing threat. The September 2025 npm supply chain attack compromised 18 packages with **2+ billion weekly downloads** (including `debug` and `chalk`), injecting a crypto-clipper that hooked into `window.ethereum` to intercept MetaMask calls and used Levenshtein distance matching to replace blockchain addresses. The December 2024 Solana `@solana/web3.js` compromise (versions 1.95.6/1.95.7) exfiltrated private keys during a 5-hour window. The 2018 `event-stream`/Copay attack specifically targeted BitPay's wallet with an AES-encrypted payload that only activated in the Copay build context. MetaMask developed [LavaMoat](https://github.com/LavaMoat/LavaMoat), an open-source toolset to defend JavaScript applications against supply chain attacks.

---

## Smart contract wallets vs. traditional wallets: fundamentally different audit targets

The distinction between smart contract wallet audits and traditional key-management wallet audits is not just scope—it's a different security paradigm. Smart contract wallets place logic on-chain where it's publicly visible, immutable (unless upgradeable, which introduces its own risks), and callable by anyone. Traditional wallets keep all logic off-chain, where vulnerabilities require device access or network compromise to exploit.

**Smart contract wallet audits** focus on access control, upgrade mechanisms, guardian/recovery systems, gas management, and signature validation. The Parity multisig hack (**$30M, 2017**) resulted from an uninitialized library contract exploitable via `delegatecall`. A second Parity incident froze **~$150M permanently** when an attacker called `selfdestruct` on the uninitialized library. OpenZeppelin found a high-severity vulnerability in Argent where wallets with no guardians could be taken over by anyone calling `executeRecovery()`—**329 wallets with ~162 ETH were at immediate risk**. The vulnerability classes unique to smart contract wallets include reentrancy, signature replay across contracts with shared owners, EIP-712 implementation flaws, delegatecall storage context confusion, proxy upgrade storage collisions, and social recovery griefing attacks.

**ERC-4337 (Account Abstraction) introduces an entirely new audit surface.** UserOperations processed by bundlers through a singleton EntryPoint contract create concentrated risk. OpenZeppelin has conducted three audits of the EntryPoint for the Ethereum Foundation. A practical [ERC-4337 audit checklist](https://github.com/aviggiano/security/blob/main/audit-checklists/ERC-4337.md) identifies critical areas: `validateUserOp` must correctly bind signatures to `userOpHash` including chain ID and EntryPoint address (omitting either enables cross-chain replay); Account Factories must use CREATE2 with deployment addresses depending on initial signatures (Biconomy's audit found attackers could gain control of counterfactual wallets otherwise); and Paymasters must resist economic drain attacks. A critical misconception found in audits: teams assume bundlers/EntryPoint act as security layers—**they don't**. They enforce protocol rules, not business logic.

**MPC wallet audits** represent a third category requiring advanced cryptographic expertise. Fireblocks' research team discovered the BitForge vulnerabilities (August 2023): zero-day flaws in GG-18/GG-20 threshold signature implementations where missing zero-knowledge proof validation during key generation allowed full private key extraction. **15+ wallet providers were affected**, including Coinbase WaaS, ZenGo, and Binance. The lesson: mathematical protocol security on paper doesn't guarantee implementation security. MPC audits must verify every required ZK proof is actually implemented and validated, key share distribution and storage isolation, communication protocol security between signing parties, and proactive share refresh mechanisms.

The following table captures the core differences:

| Dimension | Smart contract wallet | Traditional wallet | MPC wallet |
|---|---|---|---|
| **Primary target** | On-chain Solidity/Cairo code | Off-chain key management software/hardware | Cryptographic protocol implementation |
| **Attack surface** | Public, callable by anyone | Requires device/network access | Subtle mathematical/implementation flaws |
| **Expertise required** | EVM internals, formal verification | Hardware security, OS internals | Advanced cryptography, distributed systems |
| **Vulnerability persistence** | Permanent unless upgradeable | Patchable via firmware/software updates | Protocol migration required |
| **Chain dependency** | Chain-specific | Chain-agnostic | Chain-agnostic |

---

## Published audit reports and open-source resources worth studying

The security community has published extensive resources. Here are the highest-value starting points for a software engineer:

**Frameworks and checklists:**
- Trail of Bits' "[Building Secure Contracts](https://secure-contracts.com)" ([GitHub](https://github.com/crytic/building-secure-contracts)) — comprehensive smart contract security handbook with platform-specific vulnerability examples for Ethereum, Solana, Algorand, Cairo, Cosmos, and TON
- SlowMist's [Cryptocurrency Security Audit Guide](https://github.com/slowmist/Cryptocurrency-Security-Audit-Guide) — includes a [Blockchain Common Vulnerability List](https://github.com/slowmist/Cryptocurrency-Security-Audit-Guide/blob/main/Blockchain-Common-Vulnerability-List.md) and an [Account Abstraction Wallet Audit Checklist](https://slowmist.medium.com/slowmist-security-audit-checklist-for-account-abstraction-wallets-ed48fc10cdbc)
- CertiK's [Crypto Wallet Security Assessment Checklist](https://www.certik.com/resources/blog/cryptowalletsecurityassessmentchecklist) — the most detailed public checklist with separate sections for general, mobile, extension, and server-side checks
- Coinspect's [Wallet Security Verification Standard](https://github.com/coinspect/wallet-security-verification-standard) — open-source checklist covering authentication, dApp interaction, key management, and signature security
- ConsenSys Diligence's [Smart Contract Best Practices](https://consensysdiligence.github.io/smart-contract-best-practices/) — covers known attacks, development recommendations, and security tools
- The [CCSS standard](https://cryptoconsortium.org/standards-2/) — 10 security aspects, 41 controls, three certification levels

**Public wallet audit reports:**
- Safe (Gnosis Safe): all version audits at [safe-fndn/safe-smart-account/docs](https://github.com/safe-fndn/safe-smart-account/tree/main/docs)
- Argent: Ethereum audits at [argentlabs/argent-contracts/audit](https://github.com/argentlabs/argent-contracts/tree/master/audit); Starknet audits by ConsenSys Diligence and ChainSecurity
- ERC-4337 EntryPoint: [OpenZeppelin's three successive audits](https://blog.openzeppelin.com/eip-4337-ethereum-account-abstraction-incremental-audit)
- Trail of Bits' complete publications repository at [github.com/trailofbits/publications](https://github.com/trailofbits/publications) (includes WalletConnect v2.0, Squads Protocol v4, TON multisig, and Silence Labs TSS library audits)
- Least Authority's [published audits](https://leastauthority.com/security-consulting/published-audits/) (includes MetaMask seed phrase implementation, Rabby Wallet, and Wallet V)
- ConsenSys Diligence's [audit archive](https://diligence.consensys.io/audits/) (includes Wallet Guard Snap, Solflare MetaMask Snap)

**Open-source security tools:**
- [Slither](https://github.com/crytic/slither) (Trail of Bits) — Solidity/Vyper static analysis
- [Echidna](https://github.com/crytic/echidna) (Trail of Bits) — property-based smart contract fuzzer
- [Medusa](https://github.com/crytic/medusa) (Trail of Bits) — parallelized smart contract fuzzer
- [OpenZeppelin Contracts](https://github.com/OpenZeppelin/openzeppelin-contracts) — battle-tested implementations including Account Abstraction (ERC-1271) and upgradeability patterns
- [Ethernaut](https://ethernaut.openzeppelin.com) — interactive smart contract security CTF (140K+ plays)
- [LavaMoat](https://github.com/LavaMoat/LavaMoat) (MetaMask) — JavaScript supply chain defense

---

## What this means for engineers building wallet software

The convergence trend is clear: modern wallets like Safe with MPC signers, or Argent's account abstraction on Starknet, combine elements of all three audit paradigms. A comprehensive security program must cover on-chain contracts, off-chain signing infrastructure, and any cryptographic protocols in between. The most impactful takeaways for engineers learning blockchain fundamentals:

**Choose Ed25519 over ECDSA when possible.** Deterministic nonce generation eliminates the most catastrophic class of signing vulnerabilities by construction. When ECDSA (secp256k1) is required (Bitcoin, Ethereum), always use RFC 6979 deterministic nonces and verify your implementation against known test vectors.

**Treat the BIP-32 extended public key (xpub) as sensitive material.** Combined with any leaked non-hardened child private key, it reveals the master private key. Use hardened derivation for account-level paths and never expose chain codes to untrusted parties.

**Supply chain is now the dominant attack vector for software wallets.** The September 2025 npm attack, the Solana web3.js compromise, and the Copay event-stream attack all targeted the dependency graph rather than the wallet code itself. Pin dependency versions, commit lockfiles, verify build reproducibility, and consider tools like LavaMoat for JavaScript sandboxing.

**Hardware wallet security depends on the Secure Element, not the device form factor.** General-purpose microcontrollers leak secrets through power consumption and electromagnetic emissions. The difference between EAL5+ Secure Element signing and MCU signing is the difference between "requires a nation-state lab" and "requires a USB oscilloscope and a weekend."

**For smart contract wallets, the deployment transaction is part of the attack surface.** OpenZeppelin's Gnosis Safe backdoor research showed that modules injected during deployment—before owner initialization—create permanent, invisible backdoors. Audit the factory contract and initialization sequence, not just the wallet logic.