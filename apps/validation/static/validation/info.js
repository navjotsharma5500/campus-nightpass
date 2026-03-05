/* --- CONFIGURATION & HELPERS --- */
function setPassState(state) {
    const card = document.getElementById('pass-card-container'); // Ensure this ID matches your HTML
    if (!card) return;
    card.classList.remove('valid', 'invalid', 'warning');
    if (state) card.classList.add(state);
}

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

$.ajaxSetup({ headers: { "X-CSRFToken": getCookie('csrftoken') } });

function is_mobile() {
    const mobile = navigator.userAgentData ? navigator.userAgentData.mobile : /Mobi|Android/i.test(navigator.userAgent);
    toastr.options = {
        "closeButton": false,
        "positionClass": mobile ? "toast-top-right" : "toast-bottom-full-width",
        "timeOut": "3000",
        "showMethod": "fadeIn",
        "hideMethod": "fadeOut"
    };
    return mobile;
}

/* --- DISPLAY LOGIC --- */
var timeouts = [];

function resetProfile() {
    const profileCard = document.querySelector('.profile-card');
    if (profileCard) {
        profileCard.innerHTML = `
            <img class="tempimg" src="https://static.vecteezy.com/system/resources/previews/005/129/844/non_2x/profile-user-icon-isolated-on-white-background-eps10-free-vector.jpg" style="opacity:0.5;">
            <h2 style="margin-top:10px;">Waiting for Scan...</h2>
            <p>Registration: -</p>
        `;
    }
    resetPass();
}

function resetPass() {
    const passDetails = document.querySelector('.pass-details');
    if (!passDetails) return;
    
    // Clear only the info you want to keep
    passDetails.innerHTML = `
        <h2>Student Information</h2>
        <p id="ui-hostel">Hostel: -</p>
        <button id="action-button" class="action-button" style="visibility: hidden;"></button>
    `;
    setPassState(null);
}

function updateProfile(data) {
    const profileCard = document.querySelector('.profile-card');
    if (!profileCard) return;

    profileCard.innerHTML = `
        <img class="tempimg" id="user_picture" src="${data.picture}" alt="Student Picture">
        <h2 style="margin-top:10px;">${data.name}</h2>
        <p><strong>Registration:</strong> ${data.registration_number}</p>
    `;

    // Handle hostel display in the side card
    const hostelDisplay = document.getElementById('ui-hostel');
    if (hostelDisplay) {
        hostelDisplay.innerHTML = `<strong>Hostel:</strong> ${data.hostel || 'N/A'}`;
    }

    // Reset after 7 seconds
    for (var i = 0; i < timeouts.length; i++) clearTimeout(timeouts[i]);
    timeouts.push(setTimeout(resetProfile, 7000));
}

function updateUserPass(data, user_data, task, request_user_location, message) {
    const actionButton = document.getElementById('action-button');
    if (!actionButton) return;

    if (!data.pass_id) {
        setPassState('invalid');
        toastr.error(message || "No valid pass found");
        return;
    }

    // Logic for showing the action button
    if (task['check_out']) {
        actionButton.style.visibility = 'visible';
        actionButton.innerHTML = `Check Out`;
        setPassState(request_user_location === 'campus_resource' ? 'warning' : 'valid');
        actionButton.onclick = function() { checkOut(user_data.registration_number); };
        
        // Auto-click for desktop if configured previously
        if (!is_mobile() && request_user_location === 'campus_resource') {
            checkOut(user_data.registration_number);
        }
    } else if (task['check_in']) {
        actionButton.style.visibility = 'visible';
        actionButton.innerHTML = `Check In`;
        setPassState(request_user_location === 'campus_resource' ? 'valid' : 'warning');
        actionButton.onclick = function() { checkIn(user_data.registration_number); };

        if (!is_mobile() && request_user_location === 'campus_resource') {
            checkIn(user_data.registration_number);
        }
    } else {
        setPassState('invalid');
        actionButton.style.visibility = 'hidden';
    }
}

/* --- DATA ACTIONS --- */
function updateStats(data) {
    const totalEl = document.getElementById('total_count');
    const checkinEl = document.getElementById('check_in_count');
    if (totalEl) totalEl.innerHTML = 'Booking: ' + data.total_count;
    if (checkinEl) checkinEl.innerHTML = 'Checked In: ' + data.check_in_count;
}

function fetch_data(dump) {
    $.ajax({
        method: "POST",
        url: "/access/extension/fetchuser/performtask/",
        data: dump,
        dataType: "json",
        success: function (response) {
    console.log("Server Response:", response); 
    
    if (response.status) {
        // This line restores the green notification!
        toastr.success(response.message || "Action Successful");

        if (response.user) {
            // Update profile (Name & Picture)
            updateProfile(response.user);
            
            // Update Hostel specifically if it's not in your updateProfile HTML
            const hostelDisplay = document.getElementById('ui-hostel');
            if (hostelDisplay) {
                hostelDisplay.innerHTML = `<strong>Hostel:</strong> ${response.user.hostel}`;
            }
        }
        
        // Play success sound
        var audio = new Audio('/static/validation/beep.mp3');
        audio.play().catch(() => {});
        
    } else {
        // Red notification for "No active pass", etc.
        toastr.error(response.message || "Scan failed");
    }
}
    });
}

function checkIn(reg) {
    $.post("checkin/", { 'registration_number': reg }, function (response) {
        if (response.status) {
            toastr.success(response.message);
            if (response.student_stats) updateStats(response.student_stats);
            if (!is_mobile()) document.getElementById('roll_num').focus();
            setTimeout(resetProfile, 3000);
        } else {
            toastr.error(response.message);
        }
    });
}

function checkOut(reg) {
    $.post("checkout/", { 'registration_number': reg }, function (response) {
        if (response.status) {
            toastr.success(response.message);
            if (response.student_stats) updateStats(response.student_stats);
            if (!is_mobile()) document.getElementById('roll_num').focus();
            setTimeout(resetProfile, 3000);
        } else {
            toastr.error(response.message);
        }
    });
}

/* --- INPUT MONITORING --- */
function checkInput() {
    const inputElement = document.getElementById('roll_num');
    if (inputElement && inputElement.value.length === 10) {
        const val = inputElement.value;
        inputElement.value = '';
        
        fetch_data({ 'registration_number': val });
        
        var audio = new Audio('/static/validation/beep.mp3'); 
        audio.play().catch(() => console.log("Audio blocked"));
    }
}

setInterval(checkInput, 300);