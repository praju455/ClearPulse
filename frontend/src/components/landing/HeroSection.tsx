'use client';

import Link from 'next/link';
import Typewriter from '@/components/Typewriter';
import DNABackground from '@/components/landing/DNABackground';
import { Heart, Stethoscope, ChevronDown } from 'lucide-react';

export default function HeroSection() {
    return (
        <section className="relative pt-32 pb-24 px-6 overflow-hidden min-h-[90vh] flex items-center justify-center bg-[#fdfbf7]">
            {/* Animated DNA Background */}
            <DNABackground />

            {/* Floating Decorative Dots */}
            <div className="absolute inset-0 z-[1] pointer-events-none overflow-hidden select-none">
                <div className="absolute top-20 left-[10%] w-4 h-4 bg-retro-accent-pink border-2 border-black animate-float" style={{ animationDelay: '0s' }} />
                <div className="absolute top-40 right-[15%] w-3 h-3 bg-retro-accent-green border-2 border-black animate-float" style={{ animationDelay: '1s' }} />
                <div className="absolute bottom-32 left-[20%] w-5 h-5 bg-retro-accent-yellow border-2 border-black animate-float" style={{ animationDelay: '0.5s' }} />
                <div className="absolute top-60 left-[5%] w-3 h-3 bg-indigo-300 border-2 border-black animate-float" style={{ animationDelay: '1.5s' }} />
                <div className="absolute bottom-48 right-[8%] w-4 h-4 bg-retro-accent-pink border-2 border-black animate-float" style={{ animationDelay: '2s' }} />
            </div>

            <div className="max-w-5xl mx-auto text-center relative z-10">
                {/* Badge */}
                <div className="animate-fade-in-up inline-flex items-center gap-2 px-5 py-2 rounded-full bg-white/70 border-2 border-black mb-8 backdrop-blur-sm shadow-[3px_3px_0px_0px_rgba(0,0,0,1)]">
                    <span className="w-2.5 h-2.5 rounded-full bg-retro-accent-green cursor-blink" />
                    <span className="text-xs font-bold text-black uppercase tracking-widest font-mono">Decentralized Healthcare Intelligence</span>
                </div>

                {/* Title */}
                <h1 className="animate-fade-in-up delay-200 text-6xl md:text-8xl font-black leading-tight mb-6 drop-shadow-sm mix-blend-multiply text-black">
                    ClearPulse AI
                </h1>

                {/* Typewriter */}
                <div className="animate-fade-in-up delay-400 text-xl md:text-2xl text-gray-800 max-w-3xl mx-auto mb-8 font-bold font-mono h-20 flex items-center justify-center">
                    <Typewriter
                        text={[
                            "AI-powered medical analysis secured by IPFS.",
                            "Your health data, fully decentralized.",
                            "Instant AI diagnostics from your reports."
                        ]}
                        speed={40}
                        delay={3000}
                    />
                </div>

                {/* Description */}
                <p className="animate-fade-in-up delay-500 text-base text-gray-600 max-w-lg mx-auto mb-14 leading-relaxed">
                    Upload medical reports, get instant AI analysis with risk scores, chat with an AI health assistant, and watch personalized video explanations — all with patient-controlled access on-chain.
                </p>

                {/* Who Are You? - Two Doors */}
                <div className="max-w-3xl mx-auto animate-fade-in-up delay-700 mt-4">
                    <h2 className="text-sm font-bold text-black uppercase tracking-[0.2em] mb-8 font-mono">
                        <span className="border-b-3 border-black pb-1">Select Your Role</span>
                    </h2>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-8">
                        {/* Patient Card */}
                        <Link href="/patient" className="group retro-card p-8 text-center cursor-pointer relative hover:-translate-y-2 transition-all duration-300 bg-white/95 backdrop-blur-sm glow-pink">
                            <div className="w-20 h-20 border-2 border-black bg-retro-accent-pink mx-auto mb-5 flex items-center justify-center shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] group-hover:scale-110 group-hover:rotate-3 transition-all duration-300">
                                <Heart className="w-9 h-9 text-black" strokeWidth={2.5} />
                            </div>
                            <h3 className="text-2xl font-serif font-bold text-black mb-2">Patient</h3>
                            <p className="text-sm text-gray-700 font-medium leading-relaxed mb-6">
                                Upload reports, get AI analysis, talk to AI doctor, and control who sees your data.
                            </p>
                            <span className="inline-flex items-center gap-2 text-sm text-black font-bold uppercase border-b-2 border-black group-hover:bg-retro-accent-yellow/50 transition-colors px-2 py-1">
                                Get Started <span className="group-hover:translate-x-1 transition-transform inline-block">→</span>
                            </span>
                        </Link>

                        {/* Doctor Card */}
                        <Link href="/doctor" className="group retro-card p-8 text-center cursor-pointer relative hover:-translate-y-2 transition-all duration-300 bg-white/95 backdrop-blur-sm glow-green">
                            <div className="w-20 h-20 border-2 border-black bg-retro-accent-green mx-auto mb-5 flex items-center justify-center shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] group-hover:scale-110 group-hover:-rotate-3 transition-all duration-300">
                                <Stethoscope className="w-9 h-9 text-black" strokeWidth={2.5} />
                            </div>
                            <h3 className="text-2xl font-serif font-bold text-black mb-2">Doctor</h3>
                            <p className="text-sm text-gray-700 font-medium leading-relaxed mb-6">
                                View patient records shared with you and review AI-generated analysis.
                            </p>
                            <span className="inline-flex items-center gap-2 text-sm text-black font-bold uppercase border-b-2 border-black group-hover:bg-retro-accent-yellow/50 transition-colors px-2 py-1">
                                Get Started <span className="group-hover:translate-x-1 transition-transform inline-block">→</span>
                            </span>
                        </Link>
                    </div>
                </div>
            </div>

            {/* Scroll Indicator */}
            <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 animate-bounce-down">
                <div className="flex flex-col items-center gap-1 opacity-60">
                    <span className="text-[10px] font-bold uppercase tracking-widest text-black font-mono">Scroll</span>
                    <ChevronDown className="w-5 h-5 text-black" strokeWidth={3} />
                </div>
            </div>
        </section>
    );
}
