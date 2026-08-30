// Revision: a decoder-driven crossbar rather than a logarithmic barrel.
// The shift amount is one-hot decoded, and every output bit is an AND-OR
// selection over all the source bits that could land on it. Same function,
// a completely different gate structure.
module barrel8(input [7:0] a, input [2:0] s, output [7:0] y);
  wire [7:0] d;
  assign d[0] = (s == 3'd0);
  assign d[1] = (s == 3'd1);
  assign d[2] = (s == 3'd2);
  assign d[3] = (s == 3'd3);
  assign d[4] = (s == 3'd4);
  assign d[5] = (s == 3'd5);
  assign d[6] = (s == 3'd6);
  assign d[7] = (s == 3'd7);

  assign y[0] =  (d[0] & a[0]);
  assign y[1] =  (d[0] & a[1]) | (d[1] & a[0]);
  assign y[2] =  (d[0] & a[2]) | (d[1] & a[1]) | (d[2] & a[0]);
  assign y[3] =  (d[0] & a[3]) | (d[1] & a[2]) | (d[2] & a[1]) | (d[3] & a[0]);
  assign y[4] =  (d[0] & a[4]) | (d[1] & a[3]) | (d[2] & a[2]) | (d[3] & a[1])
               | (d[4] & a[0]);
  assign y[5] =  (d[0] & a[5]) | (d[1] & a[4]) | (d[2] & a[3]) | (d[3] & a[2])
               | (d[4] & a[1]) | (d[5] & a[0]);
  assign y[6] =  (d[0] & a[6]) | (d[1] & a[5]) | (d[2] & a[4]) | (d[3] & a[3])
               | (d[4] & a[2]) | (d[5] & a[1]) | (d[6] & a[0]);
  assign y[7] =  (d[0] & a[7]) | (d[1] & a[6]) | (d[2] & a[5]) | (d[3] & a[4])
               | (d[4] & a[3]) | (d[5] & a[2]) | (d[6] & a[1]) | (d[7] & a[0]);
endmodule
