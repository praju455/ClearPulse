'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import Link from 'next/link';

// ─── Types ───────────────────────────────────────────────────────────────────

interface Message {
    role: 'user' | 'assistant';
    content: string;
}

interface TriageResponse {
    response: string;
    triage_level: 'Assessing' | 'Home' | 'Clinic' | 'Emergency';
    symptom_summary: string;
    recommended_action: string;
    care_recommendations: string[];
    session_complete: boolean;
}

const SUPPORTED_LANGUAGES = [
    { code: 'en-IN', name: 'English', flag: '🇮🇳' },
    { code: 'hi-IN', name: 'Hindi', flag: '🇮🇳' },
    { code: 'ta-IN', name: 'Tamil', flag: '🇮🇳' },
    { code: 'te-IN', name: 'Telugu', flag: '🇮🇳' },
    { code: 'bn-IN', name: 'Bengali', flag: '🇮🇳' },
    { code: 'kn-IN', name: 'Kannada', flag: '🇮🇳' },
    { code: 'ml-IN', name: 'Malayalam', flag: '🇮🇳' },
    { code: 'mr-IN', name: 'Marathi', flag: '🇮🇳' },
    { code: 'gu-IN', name: 'Gujarati', flag: '🇮🇳' },
];

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

// ─── WAV encoder ─────────────────────────────────────────────────────────────
// Converts any browser-recorded blob (webm/mp4/ogg) into standard WAV PCM 16-bit
// which Sarvam STT accepts reliably on every browser.
async function convertBlobToWav(blob: Blob): Promise<Blob> {
    const audioCtx = new AudioContext({ sampleRate: 16000 });
    const arrayBuffer = await blob.arrayBuffer();
    const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
    await audioCtx.close();

    const numChannels = 1; // mono
    const sampleRate = audioBuffer.sampleRate;
    const samples = audioBuffer.getChannelData(0); // use first channel
    const length = samples.length;

    const wavBuffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(wavBuffer);

    const write = (offset: number, str: string) => {
        for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    write(0, 'RIFF');
    view.setUint32(4, 36 + length * 2, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    view.setUint32(16, 16, true);       // chunk size
    view.setUint16(20, 1, true);        // PCM
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); // byte rate
    view.setUint16(32, 2, true);        // block align
    view.setUint16(34, 16, true);       // bits per sample
    write(36, 'data');
    view.setUint32(40, length * 2, true);

    let offset = 44;
    for (let i = 0; i < length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        offset += 2;
    }

    return new Blob([wavBuffer], { type: 'audio/wav' });
}

// ─── Component ───────────────────────────────────────────────────────────────

interface TriagePanelProps {
    patientId: string;
}

export default function TriagePanel({ patientId }: TriagePanelProps) {
    const [messages, setMessages] = useState<Message[]>([
        {
            role: 'assistant',
            content: "Hello! I'm ClearPulse, your AI health assistant. I'm here to help assess your symptoms and guide you to the right care. Could you please describe what you're experiencing today?",
        },
    ]);
    const [input, setInput] = useState('');
    const [isTyping, setIsTyping] = useState(false);
    const [triageLevel, setTriageLevel] = useState<TriageResponse['triage_level']>('Assessing');
    const [recommendedAction, setRecommendedAction] = useState('');
    const [careRecommendations, setCareRecommendations] = useState<string[]>([]);
    const [symptomSummary, setSymptomSummary] = useState('');
    const [sessionComplete, setSessionComplete] = useState(false);
    const [sttError, setSttError] = useState('');

    // Voice: always attempt — backend will return 503 if not configured
    const [selectedLanguage, setSelectedLanguage] = useState('en-IN');
    const [isRecording, setIsRecording] = useState(false);
    const [ttsEnabled, setTtsEnabled] = useState(false);
    const [isPlayingAudio, setIsPlayingAudio] = useState(false);

    const mediaRecorderRef = useRef<MediaRecorder | null>(null);
    const audioChunksRef = useRef<Blob[]>([]);
    const endOfMessagesRef = useRef<HTMLDivElement>(null);
    const currentAudioRef = useRef<HTMLAudioElement | null>(null);

    useEffect(() => {
        endOfMessagesRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isTyping]);

    // ─── TTS playback ─────────────────────────────────────────────────────
    const playTTS = useCallback(async (text: string) => {
        if (!ttsEnabled) return;
        try {
            setIsPlayingAudio(true);
            const res = await fetch(`${API_URL}/api/triage/tts`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, language_code: selectedLanguage }),
            });
            if (!res.ok) { setIsPlayingAudio(false); return; }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            currentAudioRef.current = audio;
            audio.onended = () => { setIsPlayingAudio(false); URL.revokeObjectURL(url); };
            audio.onerror = () => setIsPlayingAudio(false);
            await audio.play();
        } catch {
            setIsPlayingAudio(false);
        }
    }, [ttsEnabled, selectedLanguage]);

    const stopAudio = () => {
        if (currentAudioRef.current) {
            currentAudioRef.current.pause();
            currentAudioRef.current = null;
        }
        setIsPlayingAudio(false);
    };

    // ─── STT recording ────────────────────────────────────────────────────
    const startRecording = async () => {
        setSttError('');
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

            // Pick any supported format — we'll convert to WAV before sending
            let mimeType = '';
            if (typeof MediaRecorder.isTypeSupported === 'function') {
                if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mimeType = 'audio/webm;codecs=opus';
                else if (MediaRecorder.isTypeSupported('audio/webm')) mimeType = 'audio/webm';
                else if (MediaRecorder.isTypeSupported('audio/mp4')) mimeType = 'audio/mp4';
            }

            const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
            audioChunksRef.current = [];
            recorder.ondataavailable = e => { if (e.data.size > 0) audioChunksRef.current.push(e.data); };
            recorder.onstop = async () => {
                stream.getTracks().forEach(t => t.stop());
                const rawBlob = new Blob(audioChunksRef.current, { type: recorder.mimeType || 'audio/webm' });
                if (rawBlob.size < 500) {
                    setSttError('Recording was too short. Please hold and speak clearly.');
                    return;
                }
                try {
                    // Convert to WAV — Sarvam accepts this format on every browser
                    const wavBlob = await convertBlobToWav(rawBlob);
                    await transcribeAudio(wavBlob);
                } catch (convErr) {
                    console.error('WAV conversion failed, sending raw:', convErr);
                    await transcribeAudio(rawBlob); // fallback: send raw
                }
            };
            recorder.start(100);
            mediaRecorderRef.current = recorder;
            setIsRecording(true);
        } catch (err) {
            console.error('Microphone access denied:', err);
            setSttError('Microphone access denied. Please allow microphone access in your browser.');
        }
    };

    const stopRecording = () => {
        if (mediaRecorderRef.current && isRecording) {
            mediaRecorderRef.current.stop();
            setIsRecording(false);
        }
    };

    const transcribeAudio = async (blob: Blob) => {
        try {
            setIsTyping(true);
            const formData = new FormData();
            // Always send as WAV — Sarvam STT accepts it on every browser/OS
            formData.append('audio', blob, 'recording.wav');
            formData.append('language_code', selectedLanguage);
            const res = await fetch(`${API_URL}/api/triage/stt`, {
                method: 'POST',
                body: formData,
            });
            if (res.ok) {
                const data = await res.json();
                if (data.transcript) {
                    setInput(data.transcript);
                    setSttError('');
                } else {
                    setSttError('No speech detected. Please speak clearly and try again.');
                }
            } else {
                const errData = await res.json().catch(() => ({}));
                setSttError(errData.detail || `Voice service error (${res.status}). Please type instead.`);
            }
        } catch (err) {
            console.error('STT failed:', err);
            setSttError('Could not reach voice service. Please type your symptoms.');
        } finally {
            setIsTyping(false);
        }
    };

    // ─── Send message ─────────────────────────────────────────────────────
    const handleSend = async (overrideText?: string) => {
        const text = (overrideText ?? input).trim();
        if (!text) return;
        setInput('');

        const newMessages: Message[] = [...messages, { role: 'user', content: text }];
        setMessages(newMessages);
        setIsTyping(true);

        try {
            const res = await fetch(`${API_URL}/api/triage/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    patient_id: patientId,
                    message: text,
                    history: messages.map(m => ({ role: m.role, content: m.content })),
                    language_code: selectedLanguage,
                }),
            });

            if (!res.ok) throw new Error('Triage API error');
            const data: TriageResponse = await res.json();

            setMessages(prev => [...prev, { role: 'assistant', content: data.response }]);
            setTriageLevel(data.triage_level);
            setRecommendedAction(data.recommended_action);
            setCareRecommendations(data.care_recommendations || []);
            setSymptomSummary(data.symptom_summary || '');
            setSessionComplete(data.session_complete || false);

            // Auto-play TTS for assistant response
            if (data.response) await playTTS(data.response);

        } catch {
            setMessages(prev => [
                ...prev,
                { role: 'assistant', content: "I'm sorry, I couldn't connect to the triage service. Please try again in a moment." },
            ]);
        } finally {
            setIsTyping(false);
        }
    };

    // ─── Triage level config ──────────────────────────────────────────────
    const triageLevelConfig = {
        Assessing: { bg: 'bg-gray-50', border: 'border-gray-200', text: 'text-gray-600', icon: '⏳', label: 'Evaluating Symptoms...' },
        Home: { bg: 'bg-green-50', border: 'border-green-200', text: 'text-green-700', icon: '🏡', label: 'Home Care' },
        Clinic: { bg: 'bg-amber-50', border: 'border-amber-200', text: 'text-amber-700', icon: '🩺', label: 'Clinic Visit Needed' },
        Emergency: { bg: 'bg-red-50', border: 'border-red-200', text: 'text-red-700', icon: '🚨', label: 'Emergency' },
    };
    const lvl = triageLevelConfig[triageLevel];

    return (
        <div className="grid lg:grid-cols-[1fr_380px] gap-6 h-full">
            {/* ── Left: Chat Interface ── */}
            <div className="bg-white rounded-3xl shadow-sm border border-gray-100 flex flex-col overflow-hidden" style={{ minHeight: '600px' }}>
                {/* Header */}
                <div className="bg-gradient-to-r from-blue-600 to-indigo-600 px-6 py-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 bg-white/20 rounded-full flex items-center justify-center text-xl">🏥</div>
                        <div>
                            <h2 className="font-bold text-white text-base leading-tight">ClearPulse Triage Assistant</h2>
                            <p className="text-blue-100 text-xs">AI-powered symptom assessment • Not a medical diagnosis</p>
                        </div>
                    </div>

                    {/* Language + TTS controls */}
                    <div className="flex items-center gap-2">
                        {/* Language Selector */}
                        <div className="relative">
                            <select
                                value={selectedLanguage}
                                onChange={e => setSelectedLanguage(e.target.value)}
                                className="bg-white/15 text-white text-xs font-semibold rounded-xl px-3 py-2 border border-white/20 backdrop-blur-sm appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-white/30 pr-7"
                                style={{ backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='white'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'%3E%3C/path%3E%3C/svg%3E\")", backgroundRepeat: 'no-repeat', backgroundPosition: 'right 0.5rem center', backgroundSize: '1em' }}
                            >
                                {SUPPORTED_LANGUAGES.map(l => (
                                    <option key={l.code} value={l.code} className="text-gray-900 bg-white">{l.flag} {l.name}</option>
                                ))}
                            </select>
                        </div>

                    {/* TTS Toggle — always visible */}
                        <button
                            onClick={() => { setTtsEnabled(p => !p); if (isPlayingAudio) stopAudio(); }}
                            title={ttsEnabled ? 'Disable voice output' : 'Enable voice output'}
                            className={`w-9 h-9 rounded-full flex items-center justify-center text-base transition-all border ${ttsEnabled ? 'bg-white text-blue-600 border-white shadow-sm' : 'bg-white/15 text-white border-white/20 hover:bg-white/25'}`}
                        >
                            {isPlayingAudio ? '⏹' : '🔊'}
                        </button>
                    </div>
                </div>

                {/* Messages */}
                <div className="flex-1 overflow-y-auto p-5 space-y-4">
                    {messages.map((m, i) => (
                        <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                            {m.role === 'assistant' && (
                                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-white text-xs font-bold mr-2 mt-1 shrink-0">AI</div>
                            )}
                            <div className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${m.role === 'user'
                                ? 'bg-blue-600 text-white rounded-br-none shadow-sm'
                                : 'bg-gray-50 text-gray-800 rounded-bl-none border border-gray-100'
                                }`}>
                                {m.content}
                            </div>
                        </div>
                    ))}

                    {isTyping && (
                        <div className="flex justify-start">
                            <div className="w-7 h-7 rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-white text-xs font-bold mr-2 mt-1 shrink-0">AI</div>
                            <div className="bg-gray-50 border border-gray-100 rounded-2xl rounded-bl-none px-5 py-4 flex items-center gap-1.5">
                                <div className="w-2 h-2 bg-blue-400 rounded-full animate-bounce" />
                                <div className="w-2 h-2 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: '0.15s' }} />
                                <div className="w-2 h-2 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: '0.3s' }} />
                            </div>
                        </div>
                    )}
                    <div ref={endOfMessagesRef} />
                </div>

                {/* Input */}
                <div className="p-4 border-t border-gray-100 bg-gray-50/50">
                    <div className="flex gap-2 items-end">
                        {/* Mic button — always visible */}
                        <button
                            onMouseDown={startRecording}
                            onMouseUp={stopRecording}
                            onTouchStart={startRecording}
                            onTouchEnd={stopRecording}
                            disabled={isTyping}
                            title="Hold to record voice"
                            className={`w-11 h-11 rounded-xl flex items-center justify-center text-base shrink-0 transition-all border ${isRecording
                                ? 'bg-red-500 text-white border-red-500 shadow-lg shadow-red-200 scale-110 animate-pulse'
                                : 'bg-white text-gray-500 border-gray-200 hover:border-blue-300 hover:text-blue-600 hover:bg-blue-50'
                                } disabled:opacity-40`}
                        >
                            🎤
                        </button>

                        <input
                            type="text"
                            value={input}
                            onChange={e => setInput(e.target.value)}
                            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleSend()}
                            placeholder="Type or hold 🎤 to speak your symptoms..."
                            disabled={isTyping || sessionComplete}
                            className="flex-1 bg-white border border-gray-200 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-400 transition-all disabled:opacity-50 placeholder-gray-400"
                            suppressHydrationWarning
                        />

                        <button
                            onClick={() => handleSend()}
                            disabled={!input.trim() || isTyping || sessionComplete}
                            className="w-11 h-11 bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white rounded-xl flex items-center justify-center transition-all shadow-sm shrink-0"
                        >
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                <line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" />
                            </svg>
                        </button>
                    </div>

                    {isRecording && (
                        <p className="text-xs text-red-500 font-medium mt-2 text-center animate-pulse">🔴 Recording… release to transcribe</p>
                    )}
                    {sttError && !isRecording && (
                        <p className="text-xs text-amber-600 font-medium mt-2 text-center bg-amber-50 rounded-lg py-1.5 px-3">
                            ⚠️ {sttError}
                        </p>
                    )}
                    {sessionComplete && (
                        <p className="text-xs text-gray-400 font-medium mt-2 text-center">Assessment complete. See your care plan →</p>
                    )}
                </div>
            </div>

            {/* ── Right: Assessment Panel ── */}
            <div className="space-y-4">
                {/* Triage Level Card */}
                <div className="bg-white rounded-3xl shadow-sm border border-gray-100 p-6">
                    <h3 className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-4">Triage Status</h3>

                    <div className={`rounded-2xl p-4 border flex items-center gap-3 mb-4 ${lvl.bg} ${lvl.border} ${triageLevel === 'Emergency' ? 'animate-pulse' : ''}`}>
                        <span className="text-2xl">{lvl.icon}</span>
                        <div>
                            <p className={`font-bold text-base ${lvl.text}`}>{lvl.label}</p>
                            {triageLevel === 'Assessing' && (
                                <p className="text-xs text-gray-400 mt-0.5">Gathering information...</p>
                            )}
                        </div>
                    </div>

                    {/* Recommended Action */}
                    {recommendedAction && (
                        <div className="mb-4">
                            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400 mb-2">Immediate Action</p>
                            <p className="text-sm text-gray-700 font-medium bg-gray-50 rounded-xl p-3 border border-gray-100 leading-relaxed">
                                {recommendedAction}
                            </p>
                        </div>
                    )}

                    {/* Action Buttons */}
                    {triageLevel === 'Clinic' && (
                        <Link href="/patient/book"
                            className="block w-full text-center bg-blue-600 hover:bg-blue-700 text-white font-semibold py-3 rounded-xl transition shadow-sm text-sm mb-3">
                            📅 Book Doctor Appointment
                        </Link>
                    )}
                    {triageLevel === 'Emergency' && (
                        <a href="tel:108"
                            className="flex items-center justify-center gap-2 w-full bg-red-600 hover:bg-red-700 text-white font-bold py-3 rounded-xl transition shadow-sm shadow-red-100 text-sm mb-3">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" /></svg>
                            Call 108 — Emergency Services
                        </a>
                    )}
                </div>

                {/* Care Recommendations */}
                {careRecommendations.length > 0 && (
                    <div className="bg-white rounded-3xl shadow-sm border border-gray-100 p-6">
                        <div className="flex items-center gap-2 mb-4">
                            <div className="w-8 h-8 bg-green-50 rounded-full flex items-center justify-center text-green-600">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
                            </div>
                            <h3 className="text-sm font-bold text-gray-900">Care Recommendations</h3>
                        </div>
                        <ul className="space-y-2">
                            {careRecommendations.map((rec, i) => (
                                <li key={i} className="flex items-start gap-3 p-3 bg-gray-50 rounded-xl border border-gray-100">
                                    <span className="w-5 h-5 rounded-full bg-green-100 text-green-700 text-[10px] font-bold flex items-center justify-center shrink-0 mt-0.5">{i + 1}</span>
                                    <span className="text-sm text-gray-700 leading-relaxed">{rec}</span>
                                </li>
                            ))}
                        </ul>
                    </div>
                )}

                {/* Symptom Summary */}
                {symptomSummary && (
                    <div className="bg-blue-50 rounded-3xl border border-blue-100 p-6">
                        <div className="flex items-center gap-2 mb-3">
                            <div className="w-8 h-8 bg-blue-100 rounded-full flex items-center justify-center text-blue-600">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /></svg>
                            </div>
                            <h3 className="text-sm font-bold text-blue-900">Clinical Summary</h3>
                        </div>
                        <p className="text-sm text-blue-800 leading-relaxed">{symptomSummary}</p>
                    </div>
                )}

                {/* Disclaimer */}
                <div className="bg-gray-50 rounded-2xl border border-gray-100 p-4 text-center">
                    <p className="text-[10px] text-gray-400 leading-relaxed">
                        <strong className="text-gray-500">Disclaimer:</strong> ClearPulse provides informational triage only. It does not replace professional medical advice, diagnosis, or treatment. Always consult a qualified healthcare professional.
                    </p>
                </div>
            </div>
        </div>
    );
}
