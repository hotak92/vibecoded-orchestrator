import crypto from 'node:crypto';
export function generateId() {
    return crypto.randomBytes(16).toString('hex');
}
