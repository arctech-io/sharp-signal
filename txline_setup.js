/**
 * TxLINE Free Tier Activation Script (Devnet)
 *
 * Usage:
 *   node txline_setup.js ./path-to-phantom-keypair.json
 *
 * The script:
 *   1. Gets a guest JWT from TxLINE
 *   2. Subscribes to the free World Cup tier on Solana devnet
 *   3. Signs the activation message with your wallet
 *   4. Activates the API token
 *   5. Prints credentials you can paste into Railway
 */

import * as anchor from "@coral-xyz/anchor";
import { ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Connection, PublicKey, SystemProgram, Keypair } from "@solana/web3.js";
import axios from "axios";
import nacl from "tweetnacl";
import { readFileSync } from "fs";

// ── Devnet config ──────────────────────────────────────────────────────
const ORIGIN = "https://txline-dev.txodds.com";
const API_BASE = `${ORIGIN}/api`;
const RPC = "https://api.devnet.solana.com";
const PROGRAM_ID = new PublicKey("6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J");
const TXL_MINT = new PublicKey("4Zao8ocPhmMgq7PdsYWyxvqySMGx7xb9cMftPMkEokRG");
const SERVICE_LEVEL = 1; // free tier
const DURATION_WEEKS = 4;

// ── Load wallet ────────────────────────────────────────────────────────
const keypairPath = process.argv[2];
if (!keypairPath) {
  console.error("Usage: node txline_setup.js <path-to-phantom-keypair.json>");
  process.exit(1);
}

let secretKey;
try {
  secretKey = new Uint8Array(JSON.parse(readFileSync(keypairPath, "utf-8")));
} catch {
  console.error("Could not parse keypair file. Make sure it's a JSON array of numbers.");
  process.exit(1);
}

const wallet = Keypair.fromSecretKey(secretKey);
console.log(`Using wallet: ${wallet.publicKey.toBase58()}`);

// ── Step 1: Get guest JWT ─────────────────────────────────────────────
console.log("\n[1/4] Getting guest JWT...");
const authRes = await axios.post(`${ORIGIN}/auth/guest/start`);
const jwt = authRes.data.token;
console.log("  JWT obtained");

// ── Step 2: Subscribe on-chain ─────────────────────────────────────────
console.log("\n[2/4] Subscribing to free tier on Solana devnet...");

const connection = new Connection(RPC, "confirmed");
const provider = new anchor.AnchorProvider(
  connection,
  new anchor.Wallet(wallet),
  { commitment: "confirmed" }
);
anchor.setProvider(provider);

// Load IDL from npm package
const { default: txoracleIdl } = await import("./txoracle_idl.json", { assert: { type: "json" } });
const program = new anchor.Program(txoracleIdl, provider);

const [tokenTreasuryPda] = PublicKey.findProgramAddressSync(
  [Buffer.from("token_treasury_v2")],
  program.programId
);

const tokenTreasuryVault = getAssociatedTokenAddressSync(
  TXL_MINT,
  tokenTreasuryPda,
  true,
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID
);

const [pricingMatrixPda] = PublicKey.findProgramAddressSync(
  [Buffer.from("pricing_matrix")],
  program.programId
);

const userTokenAccount = getAssociatedTokenAddressSync(
  TXL_MINT,
  wallet.publicKey,
  false,
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID
);

const txSig = await program.methods
  .subscribe(SERVICE_LEVEL, DURATION_WEEKS)
  .accounts({
    user: wallet.publicKey,
    pricingMatrix: pricingMatrixPda,
    tokenMint: TXL_MINT,
    userTokenAccount,
    tokenTreasuryVault,
    tokenTreasuryPda,
    tokenProgram: TOKEN_2022_PROGRAM_ID,
    associatedTokenProgram: ASSOCIATED_TOKEN_PROGRAM_ID,
    systemProgram: SystemProgram.programId,
  })
  .rpc();

console.log(`  Transaction: ${txSig}`);

// ── Step 3: Sign activation message ────────────────────────────────────
console.log("\n[3/4] Signing activation message...");

const SELECTED_LEAGUES = []; // empty = standard free bundle
const messageStr = `${txSig}:${SELECTED_LEAGUES.join(",")}:${jwt}`;
const messageBytes = new TextEncoder().encode(messageStr);
const signature = nacl.sign.detached(messageBytes, wallet.secretKey);
const walletSignature = Buffer.from(signature).toString("base64");

// ── Step 4: Activate API token ─────────────────────────────────────────
console.log("\n[4/4] Activating API token...");

const activationRes = await axios.post(
  `${API_BASE}/token/activate`,
  { txSig, walletSignature, leagues: SELECTED_LEAGUES },
  { headers: { Authorization: `Bearer ${jwt}` } }
);

const apiToken = activationRes.data.token || activationRes.data;

// ── Done ───────────────────────────────────────────────────────────────
console.log("\n═══════════════════════════════════════════");
console.log("  SET THESE IN RAILWAY DASHBOARD:");
console.log("═══════════════════════════════════════════");
console.log(`TXLINE_API_KEY=${jwt}`);
console.log(`TXLINE_BASE_URL=${API_BASE}/`);
console.log("═══════════════════════════════════════════");
