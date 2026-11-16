// Pairing and encryption, with Windows' own crypto (CNG); see secure.cpp.
//   - Each PC has a device key (ECDH P-256), made once and kept DPAPI-protected in HKCU.
//   - Every connection starts with a handshake: both send their device key and a fresh one-time key;
//     three key agreements give session keys only the two real PCs can compute (and a recording can't
//     be decrypted later, even with a stolen device key).
//   - PCs that haven't met are paired once: both show the same 6-digit code, the user confirms on
//     each. Afterwards they recognise each other's device key.
//   - Control messages (input, commands, stats) are AES-256-GCM; video, sound, mic and camera are
//     AES-256-GCM per RTP packet (RtpCipher), each stream and direction with its own key.
#pragma once

#include <cstdint>
#include <functional>
#include <string>

namespace lfrd {

class Connection;

// A media stream's key: AES-256 key + 12-byte salt (+ 2 unused; the size SRTP keys had).
constexpr size_t kSrtpKeyBytes = 46;
struct SessionKeys {
  uint8_t video[kSrtpKeyBytes];   // sender -> receiver
  uint8_t audio[kSrtpKeyBytes];   // sender -> receiver
  uint8_t mic[kSrtpKeyBytes];     // receiver -> sender
  uint8_t camera[kSrtpKeyBytes];  // receiver -> sender
};

// AES-256-GCM for one direction of the control channel (Connection seals every message with it).
class AeadKey {
 public:
  explicit AeadKey(const uint8_t key[32]);
  ~AeadKey();
  AeadKey(const AeadKey&) = delete;
  AeadKey& operator=(const AeadKey&) = delete;
  // out gets n + 16 bytes (ciphertext, tag). The counter is the nonce: never reuse one with a key.
  bool Seal(uint64_t counter, const uint8_t* in, size_t n, uint8_t* out);
  // in is ciphertext + tag (n bytes); out gets n - 16. False if it was tampered with.
  bool Open(uint64_t counter, const uint8_t* in, size_t n, uint8_t* out);
  // The same with a caller-made nonce and data that is authenticated but not encrypted (aad).
  bool Seal(const uint8_t nonce[12], const uint8_t* aad, size_t aadLen, const uint8_t* in, size_t n, uint8_t* out);
  bool Open(const uint8_t nonce[12], const uint8_t* aad, size_t aadLen, const uint8_t* in, size_t n, uint8_t* out);

 private:
  void* key_ = nullptr;  // BCRYPT_KEY_HANDLE
};

// This PC's device key fingerprint: SHA-256 of its public key, hex. Beacons carry its first 16 chars.
std::string DeviceFingerprint();
// fp may be a full fingerprint or a beacon's 16-char prefix. This PC itself counts as paired.
bool IsPaired(const std::string& fp);
// Forgets every paired PC (they'll be asked for the code again).
void ForgetPairings();

// Asked when an unknown PC connects: true = the user saw the same code on both PCs and accepts.
// The panel sets one; without it, a console asks on stdin (and anything else declines).
using PairPrompt = std::function<bool(const std::string& peerName, const std::string& code)>;
void SetPairPrompt(PairPrompt prompt);

// Strict by default: an unknown PC is refused without even asking, unless "Pair a new PC" was
// clicked on this PC within the last `seconds` (OpenPairing). Paired PCs are not affected. (A console
// copy, which has no panel, asks on stdin instead.)
void OpenPairing(int seconds);
bool PairingOpen();

// Runs on a new control connection before anything else, on both PCs (initiator = the sharing PC,
// which connected). Agrees on keys, checks the other PC's device key, pairs if needed, then switches
// conn to encrypted messages. False (with a message printed) means: drop the connection.
bool SecureHandshake(Connection& conn, bool initiator, SessionKeys& keys, std::string& peerName);

// AES-256-GCM for one media stream's RTP packets (same idea as SRTP's AEAD mode): the RTP header stays
// readable (loss and timing statistics use it) but is authenticated, the payload is encrypted and a
// 16-byte tag is appended. The nonce is the stream's salt XOR (SSRC, 48-bit packet index), so it is
// never reused. Through CNG, i.e. the CPU's AES instructions: libsrtp's software AES was measured at
// ~41 MB/s here, a real load next to 40 Mbps video on a small PC.
class RtpCipher {
 public:
  explicit RtpCipher(const uint8_t key[kSrtpKeyBytes]);  // 32-byte key + 12-byte salt
  // Sender: encrypts one RTP packet (n bytes) into out (n + 16 bytes). Packets of one SSRC must come
  // in sending order (the index counts sequence-number wraps).
  bool Seal(const uint8_t* packet, size_t n, uint8_t* out);
  // Receiver: decrypts one packet (n bytes, tag included) into out (n - 16). False = forged,
  // corrupted, or a replay of an old packet: drop it.
  bool Open(const uint8_t* packet, size_t n, uint8_t* out);

 private:
  struct Stream {
    uint16_t seq = 0;
    uint64_t roc = 0;  // sequence-number wraps so far
    bool started = false;
  };
  Stream* Find(uint32_t ssrc);
  Stream& For(uint32_t ssrc);  // found, or a new one in the oldest slot
  void Nonce(uint32_t ssrc, uint64_t index, uint8_t nonce[12]) const;
  AeadKey key_;
  uint8_t salt_[12];
  uint32_t ssrc_[4] = {};  // a few recent SSRCs (the sender's video gets a new one on mirror <-> extend)
  Stream streams_[4];
  int next_ = 0;
};

// The RTP header length (fixed part, CSRCs and extension), or 0 if `p` isn't an RTP packet.
size_t RtpHeaderLength(const uint8_t* p, size_t n);

}  // namespace lfrd
