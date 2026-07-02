#!/usr/bin/env python3
"""
Friend Resume Generator - ntfy Listener
Listens for job IDs from Ahmed and automatically generates tailored resumes
"""

import json
import re
import sys
import time
import requests
import subprocess
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

TOPIC_IN = "ahmed-jobs-in-f859b712"
TOPIC_OUT = "ahmed-jobs-out-21e4b396"
NTFY_URL = "https://ntfy.sh"
PROFILE = "friend"

def is_job_id(message):
    """Check if message looks like a job ID."""
    return bool(re.match(r'^(li|foundit)-\d+$', message.strip()))

def send_ntfy(message, title=None, file_path=None):
    """Send message or file to OUT topic."""
    url = f"{NTFY_URL}/{TOPIC_OUT}"
    
    if file_path:
        with open(file_path, 'rb') as f:
            headers = {}
            if title:
                headers['Title'] = title
            headers['Filename'] = Path(file_path).name
            resp = requests.put(url, data=f, headers=headers)
            return resp.ok
    else:
        headers = {'Content-Type': 'text/plain; charset=utf-8'}
        if title:
            headers['Title'] = title
        resp = requests.post(url, data=message.encode('utf-8'), headers=headers)
        return resp.ok



def generate_ai_cover_letter(job, profile):
    """Generate a detailed, personalized cover letter using Groq AI."""
    
    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        print("   ⚠️  No GROQ_API_KEY found, using template")
        return None
    
    # Build context from profile
    years_exp = "5+"
    key_skills = ", ".join(profile.get('skills', {}).get('iOS Development', [])[:5])
    recent_job = profile.get('experience', [{}])[0]
    
    prompt = f"""Write a professional cover letter for this job application:

Job Title: {job.get('title', 'iOS Developer')}
Company: {job.get('company', 'the company')}
Location: {job.get('location', 'UAE')}
Description: {job.get('description', '')[:500]}

Candidate Profile:
Name: {profile['name']}
Summary: {profile.get('summary', '')}
Years of Experience: {years_exp}
Key Skills: {key_skills}
Current/Recent Position: {recent_job.get('title', '')} at {recent_job.get('company', '')}

Write a compelling 3-4 paragraph cover letter that:
1. Opens with enthusiasm for the specific role and company
2. Highlights 2-3 specific achievements and technical skills relevant to this job
3. Shows understanding of the company's needs
4. Closes with a strong call to action

Keep it professional, specific, and under 400 words. Use the candidate's actual experience and skills.
Format as plain text paragraphs (no "Dear Hiring Manager" - I'll add that).
"""
    
    try:
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [
                    {'role': 'system', 'content': 'You are an expert cover letter writer specializing in tech jobs.'},
                    {'role': 'user', 'content': prompt}
                ],
                'temperature': 0.7,
                'max_tokens': 800
            },
            timeout=30
        )
        
        if response.status_code == 200:
            ai_text = response.json()['choices'][0]['message']['content'].strip()
            print("   ✅ AI-generated cover letter created")
            return ai_text
        else:
            print(f"   ⚠️  Groq API error: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"   ⚠️  AI generation failed: {e}")
        return None


def tailor_and_generate(job_id):
    """Generate tailored resume + cover letter for job."""
    
    print(f"📝 Processing job: {job_id}")
    
    # Get job details
    result = subprocess.run(
        ["/home/ala/JobHunter/.venv/bin/python3", "scraper.py", "--profile", PROFILE, "--get-job", job_id],
        capture_output=True,
        text=True,
        cwd="/home/ala/JobHunter"
    )
    
    if result.returncode != 0:
        print(f"❌ Failed to get job details: {result.stderr}")
        return False
    
    job = json.loads(result.stdout)
    company = job.get('company', 'Company').replace(' ', '_').replace('/', '_')
    title = job.get('title', 'Position')
    
    print(f"   Job: {title} @ {job['company']}")
    
    # Load master profile
    with open('/home/ala/JobHunter/data/friend/master-profile.json') as f:
        profile = json.load(f)
    
    # Create output directory
    output_dir = Path('/home/ala/JobHunter/data/friend/output')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Tailor resume for iOS position
    # Check if it's iOS-related
    is_ios = 'ios' in title.lower() or 'ios' in job.get('description', '').lower()
    
    # Build skills dict - only include sections with content
    tailored_skills = {}
    
    if is_ios:
        # iOS-focused resume - filter out Android/Cross-Platform
        for category, skills_list in profile['skills'].items():
            if not skills_list:  # Skip empty categories
                continue
            if category in ['Android Development', 'Cross-Platform']:
                continue  # Skip for iOS jobs
            tailored_skills[category] = skills_list
    else:
        # General mobile resume - include all non-empty categories
        for category, skills_list in profile['skills'].items():
            if skills_list:  # Only include if not empty
                tailored_skills[category] = skills_list
    
    tailored_resume = {
        "name": profile["name"],
        "headline": profile["headline"],
        "email": profile["email"],
        "phone": profile["phone"],
        "linkedin": profile["linkedin"],
        "location": profile.get("location", "UAE"),
        "summary": profile.get("summary", ""),
        "certifications": profile.get("certifications", []),
        "skills": tailored_skills,  # Only non-empty, relevant sections
        "experience": profile["experience"],
        "education": profile["education"]
    }
    
    resume_json = output_dir / f"resume_{company}_{job_id}.json"
    with open(resume_json, 'w') as f:
        json.dump(tailored_resume, f, indent=2)
    
    # Generate AI-powered cover letter
    ai_paragraphs = generate_ai_cover_letter(job, profile)
    
    if ai_paragraphs:
        # Use AI-generated content
        paragraphs = ["Dear Hiring Manager,"] + [p.strip() for p in ai_paragraphs.split('\n\n') if p.strip()] + [f"Sincerely,\n{profile['name']}"]
    else:
        # Fallback to enhanced template
        paragraphs = [
            "Dear Hiring Manager,",
            f"I am writing to express my strong interest in the {title} position at {job['company']}. With over 5 years of experience in mobile development and a proven track record of delivering production-ready applications with millions of downloads, I am excited about the opportunity to contribute to your team.",
            f"As a Senior iOS Developer, I have successfully published multiple apps to the App Store and led development projects from concept to deployment. My expertise in Swift, SwiftUI, UIKit, and modern iOS architectures like TCA (The Composable Architecture) enables me to build scalable, performant applications. I have worked across diverse domains including Finance, Healthcare, Education, and Media & Entertainment, which has given me a broad perspective on solving complex technical challenges.",
            f"At {job['company']}, I am particularly drawn to the opportunity to work on {'innovative mobile solutions' if 'description' not in job else 'the challenges mentioned in your job description'}. I am confident that my experience with {', '.join(profile.get('skills', {}).get('iOS Development', [])[:3])} and my collaborative approach to development would make me a valuable addition to your team.",
            "I would welcome the opportunity to discuss how my technical skills and experience align with your needs. Thank you for considering my application, and I look forward to the possibility of contributing to your team's success.",
            f"Sincerely,\n{profile['name']}"
        ]
    
    cover_letter = {
        "name": profile["name"],
        "contact": f"{profile['email']} | {profile['phone']}",
        "date": datetime.now().strftime("%B %d, %Y"),
        "recipient": f"Hiring Manager\n{job['company']}\n{job.get('location', 'UAE')}",
        "subject": f"Application for {title}",
        "paragraphs": paragraphs
    }
    
    cover_json = output_dir / f"cover_{company}_{job_id}.json"
    with open(cover_json, 'w') as f:
        json.dump(cover_letter, f, indent=2)
    
    # Render PDFs
    resume_pdf = output_dir / f"Resume_{profile['name'].replace(' ', '_')}_{company}.pdf"
    cover_pdf = output_dir / f"CoverLetter_{profile['name'].replace(' ', '_')}_{company}.pdf"
    
    subprocess.run([
        "/home/ala/JobHunter/.venv/bin/python3", "render_pdf.py", "resume", str(resume_json), str(resume_pdf)
    ], cwd="/home/ala/JobHunter", check=True)
    
    subprocess.run([
        "/home/ala/JobHunter/.venv/bin/python3", "render_pdf.py", "cover", str(cover_json), str(cover_pdf)
    ], cwd="/home/ala/JobHunter", check=True)
    
    print(f"   ✅ PDFs generated")
    
    # Send PDFs
    send_ntfy(None, f"Resume - {job['company']}", str(resume_pdf))
    time.sleep(1)
    send_ntfy(None, f"Cover Letter - {job['company']}", str(cover_pdf))
    time.sleep(1)
    send_ntfy(
        f"Documents ready for {title} @ {job['company']}!\n\nGood luck with your application! 🚀",
        f"Documents Ready"
    )
    
    # Mark as interested
    subprocess.run([
        "/home/ala/JobHunter/.venv/bin/python3", "scraper.py", "--profile", PROFILE,
        "--mark-interested", job_id
    ], cwd="/home/ala/JobHunter")
    
    print(f"   ✅ Complete!")
    return True

def main():
    """Main listener loop."""
    print(f"🎧 Friend Resume Generator - Listening on {TOPIC_IN}")
    print(f"📤 Sending results to {TOPIC_OUT}")
    print("✅ Service started!\n")
    
    url = f"{NTFY_URL}/{TOPIC_IN}/json"
    
    while True:
        try:
            resp = requests.get(url, stream=True, timeout=None)
            
            for line in resp.iter_lines():
                if not line:
                    continue
                
                try:
                    data = json.loads(line.decode('utf-8'))
                    
                    if data.get('event') != 'message':
                        continue
                    
                    message = data.get('message', '').strip()
                    
                    if not message:
                        continue
                    
                    print(f"\n📩 Received: {message}")
                    
                    if not is_job_id(message):
                        print("   ⏭️  Not a job ID, ignoring")
                        continue
                    
                    print("   ✅ Valid job ID detected!")
                    
                    # Send acknowledgment
                    send_ntfy(
                        f"⏳ Processing your request for job {message}...\n\nGenerating tailored documents!",
                        "Job Helper - Processing"
                    )
                    
                    # Process the job
                    success = tailor_and_generate(message)
                    
                    if not success:
                        send_ntfy(
                            f"❌ Sorry, failed to process job {message}. Please try again or contact support.",
                            "Error"
                        )
                
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"⚠️  Error processing message: {e}")
                    continue
        
        except KeyboardInterrupt:
            print("\n\n👋 Shutting down listener")
            sys.exit(0)
        except Exception as e:
            print(f"⚠️  Connection error: {e}")
            print("🔄 Reconnecting in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    main()
