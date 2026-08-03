import socketio

sio = socketio.Client()

@sio.event
def connect():
    print("Connected without token!")
    #Try sending the unauthenticated bid

    sio.emit(
        'place_bid',{
            "player_id": 76,
            "bid_amount": 50000,
            'team_id': 2
        }
    )
    print("Sent 'place_bid' payload." )

@sio.on('error')
def on_error(data):
    print("Received Error: ",data)

if __name__  == '__main__':
    #Connect to your backend running on localhost 
    sio.connect('http://localhost:5000')
    sio.wait()

