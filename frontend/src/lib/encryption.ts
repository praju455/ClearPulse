/**
 * Client-Side AES-256 Encryption Utility
 * 
 * Zero-Trust Model: Files are encrypted on the client before upload.
 * The encryption key is NEVER stored on the server — only the user controls it.
 */

import CryptoJS from 'crypto-js';

const MIME_TYPES_BY_EXTENSION: Record<string, string> = {
    pdf: 'application/pdf',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    png: 'image/png',
    webp: 'image/webp',
};

/** Infer the browser content type for the report formats accepted by the uploader. */
export function getMimeTypeFromFileName(fileName: string): string {
    const extension = fileName.split('.').pop()?.toLowerCase() || '';
    return MIME_TYPES_BY_EXTENSION[extension] || 'application/octet-stream';
}

function detectMimeType(bytes: Uint8Array): string | null {
    if (bytes.length >= 5 && String.fromCharCode(...bytes.slice(0, 5)) === '%PDF-') {
        return 'application/pdf';
    }
    if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
        return 'image/jpeg';
    }
    if (
        bytes.length >= 8 &&
        bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47 &&
        bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a
    ) {
        return 'image/png';
    }
    if (
        bytes.length >= 12 &&
        String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF' &&
        String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP'
    ) {
        return 'image/webp';
    }
    return null;
}

/**
 * Generate a cryptographically random encryption key (256-bit).
 * Returns a hex string.
 */
export function generateEncryptionKey(): string {
    const key = CryptoJS.lib.WordArray.random(32); // CryptoJS expects bytes: 32 bytes = 256 bits
    return key.toString(CryptoJS.enc.Hex);
}

/**
 * Encrypt a file's ArrayBuffer with AES-256-CBC.
 * Returns the ciphertext as a base64 string and the IV used.
 */
export function encryptData(data: ArrayBuffer, key: string): { ciphertext: string; iv: string } {
    const wordArray = CryptoJS.lib.WordArray.create(data as any);
    const iv = CryptoJS.lib.WordArray.random(16); // 128-bit IV

    const encrypted = CryptoJS.AES.encrypt(wordArray, CryptoJS.enc.Hex.parse(key), {
        iv,
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7,
    });

    return {
        ciphertext: encrypted.toString(), // base64 encoded
        iv: iv.toString(CryptoJS.enc.Hex),
    };
}

/**
 * Decrypt a base64 ciphertext back to a UTF-8 string (for text-based files / analysis).
 * Used server-side or when the user wants to view their own file.
 */
export function decryptData(ciphertext: string, key: string, iv: string): string {
    const decrypted = CryptoJS.AES.decrypt(ciphertext, CryptoJS.enc.Hex.parse(key), {
        iv: CryptoJS.enc.Hex.parse(iv),
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7,
    });

    return decrypted.toString(CryptoJS.enc.Utf8);
}

/**
 * Decrypt ciphertext back to raw bytes (Uint8Array) — for binary files.
 */
export function decryptToBytes(ciphertext: string, key: string, iv: string): Uint8Array {
    const decrypted = CryptoJS.AES.decrypt(ciphertext, CryptoJS.enc.Hex.parse(key), {
        iv: CryptoJS.enc.Hex.parse(iv),
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7,
    });

    const words = decrypted.words;
    const sigBytes = decrypted.sigBytes;
    const bytes = new Uint8Array(sigBytes);

    for (let i = 0; i < sigBytes; i++) {
        bytes[i] = (words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
    }

    return bytes;
}

/**
 * Hash the encryption key (SHA-256) for verification purposes.
 * This can be stored in the database to verify a key matches without storing the key itself.
 */
export function hashKey(key: string): string {
    return CryptoJS.SHA256(key).toString(CryptoJS.enc.Hex);
}

/**
 * Encrypt a File object and return a Blob ready for upload.
 * Prepends the IV to the encrypted data for easy retrieval.
 */
export async function encryptFile(file: File, key: string): Promise<{ encryptedBlob: Blob; iv: string; keyHash: string }> {
    const buffer = await file.arrayBuffer();
    const { ciphertext, iv } = encryptData(buffer, key);

    // Create a blob with the encrypted data
    const encryptedBlob = new Blob([ciphertext], { type: 'application/octet-stream' });

    return {
        encryptedBlob,
        iv,
        keyHash: hashKey(key),
    };
}

/**
 * Fetch a file from IPFS, decrypt it, and return a Blob.
 */
export async function decryptFileFromIPFS(
    cid: string,
    key: string,
    iv: string,
    mimeType: string = 'application/octet-stream',
): Promise<Blob> {
    const normalizedKey = key.trim();
    const normalizedIv = iv.trim();
    // Keys generated before this fix were accidentally 8 bytes (16 hex
    // characters). Continue accepting them so existing encrypted reports open,
    // while all newly generated keys are the intended 256-bit length.
    if (!/^(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{64})$/.test(normalizedKey)) {
        throw new Error('The decryption key format is invalid.');
    }
    if (!/^[0-9a-fA-F]{32}$/.test(normalizedIv)) {
        throw new Error('This record has an invalid encryption IV and cannot be decrypted.');
    }

    // 1. Fetch from IPFS (using public gateway for now, or configured one)
    const gateway = "https://gateway.pinata.cloud/ipfs";
    const response = await fetch(`${gateway}/${cid}`);

    if (!response.ok) {
        throw new Error(`Failed to fetch from IPFS: ${response.statusText}`);
    }

    const ciphertext = (await response.text()).trim();
    if (!ciphertext) {
        throw new Error('The encrypted report downloaded from IPFS is empty.');
    }

    // 2. Decrypt
    const decryptedArgs = CryptoJS.AES.decrypt(ciphertext, CryptoJS.enc.Hex.parse(normalizedKey), {
        iv: CryptoJS.enc.Hex.parse(normalizedIv),
        mode: CryptoJS.mode.CBC,
        padding: CryptoJS.pad.Pkcs7,
    });

    // 3. Convert to TypedArray
    const words = decryptedArgs.words;
    const sigBytes = decryptedArgs.sigBytes;
    if (sigBytes <= 0) {
        throw new Error('The decryption key is incorrect or the encrypted report is corrupted.');
    }

    const u8 = new Uint8Array(sigBytes);
    for (let i = 0; i < sigBytes; i++) {
        u8[i] = (words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
    }

    // 4. Verify the decrypted file and use its real content type. This prevents
    // images from being sent to Chrome's PDF viewer as application/pdf.
    const detectedMimeType = detectMimeType(u8);
    if (!detectedMimeType) {
        throw new Error('The decryption key is incorrect or the report format is unsupported.');
    }
    if (mimeType !== 'application/octet-stream' && detectedMimeType !== mimeType) {
        throw new Error('The decrypted file does not match the original report type.');
    }

    return new Blob([u8], { type: detectedMimeType });
}
