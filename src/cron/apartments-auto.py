import schedule
import time

def job():
    print("Execute scraping task for ....")

schedule.every(24).hours.do(job)

# while True:
#     schedule.run_pending()       # what does that even mean?
#     time.sleep(1)                # in seconds?



if __name__ == "__main__": 
    job()