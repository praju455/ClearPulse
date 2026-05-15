'use client';

import { useUser, SignInButton } from '@clerk/nextjs';
import TriagePanel from '@/app/patient/components/TriagePanel';

export default function TriagePage() {
    const { isLoaded, isSignedIn, user } = useUser();

    if (!isLoaded) {
        return (
            <div className="min-h-screen bg-[#FAFAFA] pt-24 flex items-center justify-center">
                <div className="w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full animate-spin" />
            </div>
        );
    }

    if (!isSignedIn) {
        return (
            <div className="min-h-screen bg-[#FAFAFA] pt-24 px-6 flex items-center justify-center">
                <div className="bg-white p-8 rounded-3xl shadow-sm border border-gray-200 text-center max-w-md">
                    <div className="w-16 h-16 bg-blue-50 text-blue-600 rounded-full flex items-center justify-center mx-auto mb-4 text-3xl">
                        🏥
                    </div>
                    <h2 className="text-2xl font-bold text-gray-900 mb-2">Triage Assistant</h2>
                    <p className="text-gray-500 mb-6">
                        Please sign in to access the AI Triage Assistant with voice support.
                    </p>
                    <SignInButton mode="modal">
                        <button className="bg-blue-600 hover:bg-blue-700 text-white font-semibold py-3 px-6 rounded-xl transition w-full">
                            Sign In to Start
                        </button>
                    </SignInButton>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-[#FAFAFA] pt-24 pb-8 px-4">
            <div className="max-w-7xl mx-auto">
                <div className="mb-6">
                    <h1 className="text-2xl font-extrabold text-gray-900 tracking-tight">AI Symptom Triage</h1>
                    <p className="text-sm text-gray-500 mt-1">AI-powered assessment · Voice-enabled · Not a medical diagnosis</p>
                </div>
                <TriagePanel patientId={user?.id || 'anonymous'} />
            </div>
        </div>
    );
}
