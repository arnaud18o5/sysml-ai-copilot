package Drone {
    port def PowerPort;

    part def Battery {
        out port power : PowerPort;
        attribute capacity : Real = 5000;
    }

    part def Motor {
        in port power : PowerPort;
    }

    part def Airframe {
        part battery : Batery;
        part motors : Motor[4];

        connect battery.power to motors.power;
    }

    requirement def Endurance {
        doc /* The drone shall fly for at least 25 minutes. */
    }

    satisfy Endurance by Airframe.batery;
}
