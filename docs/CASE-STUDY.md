# Case study: from an owner's problem to production

I built Lisa to get real hands-on reps and take something all the way to production. The starting point was a concrete problem from a Danish driving school owner: students text him to book, move and cancel lessons, and he answers between lessons and in the evening. Without notifications, some students forgot their bookings and some bookings were misunderstood, which led to conflicts. So Lisa sent a confirmation when a lesson was booked and a reminder 24 hours before.

## What the owner taught me

- **His calendar is sacred.** He wanted SMS handled without him and his cancellation policy enforced, but nothing written to his own calendar without consent. That became a hard rule in the code: Lisa writes only to a dedicated "Lisa" calendar and refuses deletes anywhere else.
- **The two sides don't speak the same language, so a shared dictionary is mandatory.** The school counts in "lektioner". Students write "køretime", and to them "timer" reads as hours. An agent in the middle needs one agreed term per concept, set with the owner, and the students' phrasings mapped onto it. Otherwise the agent answers correctly in words nobody uses.
- **People only switch when the pain of the current solution is bigger than the cost of switching.** The owner wanted Lisa for all his instructors, but 3 of 4 ran paper calendars and saw no reason to change. Owners who answer texts by hand said the same. Their current way costs them little that they notice, while switching means a new tool, new habits and a risk of mistakes. A better solution doesn't win that trade on its own.

## How it ran

Lisa ran on Railway with a Danish Twilio number and a beta allowlist of phone numbers. It sent at most three automated messages per student per day. Claude classifies each message into one of 11 intents. Below 0.7 confidence the code treats it as unknown, and two unknowns in a row go to the owner. Free slots come from the owner's iCloud calendar over CalDAV.

## What broke in production, and what I changed

1. **Lisa sent students to the phone.** In the owner's first hour with the live number, FAQ answers told students to call the school instead of booking by SMS. The school's number appeared in four FAQ entries, and the prompt never said that Lisa *is* the booking channel. Fix: booking questions now go into the booking flow.
2. **The same 07:15 slot, three times.** When a student narrowed the time ("lidt senere") without naming a day, the time filter was silently dropped. Fix: search every weekday and let the time filter do the narrowing.
3. **No reply at all.** Lisa refreshed the calendar inside the SMS request. A slow iCloud call pushed it past Twilio's 15 second timeout, and Twilio dropped the reply. Fix: refresh in the background and always answer from the cache.
4. **A booking in the past.** On a ride-along, Lisa offered a slot from 30 minutes earlier, and the booking went through. The "today" path never checked the current time, and the tests missed it because they ran on fixed historical dates. Fix: filter out past slots at the one place every slot search passes through.
5. **"Sunday" meant the wrong Sunday.** In testing on the live number, "søndag kl 09" got a slot two Sundays out, because this Sunday morning was taken and the search simply took the first matching weekday. Fix: resolve "denne søndag", "i morgen" and "næste fredag" to an actual date using today's date. If that day is full, say so plainly ("Ingen ledige tider 10/5. Nærmeste: …") instead of quietly moving to another week.

Each fix came with a regression test.

## What I'd do differently

- **Ask about the users' tools, and what the current way costs them, before building.** One question about paper versus digital calendars at the start would have kept the multi-instructor feature off the roadmap.
- **Test against the real clock, not just fixed dates.** The past-slot bug lived in that gap.
